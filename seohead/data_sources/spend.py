"""Track paid-provider usage: what was charged, for which operation, and when.

A local ledger is necessary even when a provider exposes account history. Informal estimates can
drift from actual usage, and a parsing failure must not hide a charge. Two rules follow:

1. **Record a charge as soon as the provider returns its cost**, before parsing the result. A
   parser failure must not turn a paid response into an untraceable expense.
2. **Record the task identifier as well.** Arsenkin results can be fetched again by ``task_id``
   without paying twice. The ledger is therefore also an index of already-paid tasks.

The format is JSONL, one line per call. Appending a line is atomic, so an interrupted process does
not corrupt prior entries. The default path is ``~/.config/seohead/spend.jsonl`` and can be
overridden with ``SEOHEAD_SPEND_LOG``.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def log_path() -> Path:
    override = os.environ.get("SEOHEAD_SPEND_LOG")
    if override:
        return Path(override).expanduser()
    return Path(os.path.expanduser("~/.config/seohead/spend.jsonl"))


def record(
    source: str,
    operation: str,
    *,
    cost: float = 0.0,
    unit: str = "limits",
    task_id: Any | None = None,
    items: int = 0,
    extra: dict | None = None,
) -> dict:
    """Record one charge and return the stored entry.

    ``cost`` is the amount charged in the provider's own ``unit``: Arsenkin uses limit credits,
    while Yandex Cloud uses requests. The ledger deliberately does not convert usage to money.
    Prices change; the log must remain an accurate record of measured provider units.
    """
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source,
        "operation": operation,
        "cost": cost,
        "unit": unit,
        "items": items,
    }
    if task_id is not None:
        entry["task_id"] = task_id
    if extra:
        entry["extra"] = extra

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_all() -> list[dict]:
    path = log_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue  # Preserve usable history even when one JSONL line is malformed.
    return rows


def _is_uncertain(row: dict) -> bool:
    """A receipt whose cost or charge status is not actually known.

    A response can be received and still be unusable: a malformed body, for instance, proves a
    request reached the provider without proving what it cost. Such a row must never be folded
    into ``by_source``/``by_operation``/``by_day`` alongside confirmed zero-cost calls — that
    would silently relabel "unmeasured" as "measured and free". Callers set either flag on
    ``extra`` for this: ``cost_unknown`` (used here for DataForSEO) or ``charge_uncertain`` (used
    by Yandex Cloud) both mean the same thing.
    """
    extra = row.get("extra") or {}
    return bool(extra.get("cost_unknown") or extra.get("charge_uncertain"))


def report(since: str | None = None) -> dict:
    """Summarize usage by provider, operation, and day.

    ``since`` is an inclusive ``YYYY-MM-DD`` date. Rows with an uncertain cost or charge status
    (see :func:`_is_uncertain`) are kept out of the cost totals and listed separately under
    ``uncertain``, so a receipt that only proves a request reached the provider never counts as a
    measured zero-cost call.
    """
    rows = [r for r in read_all() if not since or r.get("at", "")[:10] >= since]
    uncertain = [r for r in rows if _is_uncertain(r)]
    measured = [r for r in rows if not _is_uncertain(r)]

    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_operation: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for row in measured:
        unit = row.get("unit", "limits")
        cost = float(row.get("cost") or 0)
        by_source[row.get("source", "?")][unit] += cost
        by_operation[f"{row.get('source', '?')}.{row.get('operation', '?')}"][unit] += cost
        by_day[row.get("at", "")[:10]][unit] += cost

    return {
        "ok": True,
        "calls": len(rows),
        "since": since,
        "by_source": {k: dict(v) for k, v in by_source.items()},
        "by_operation": {k: dict(v) for k, v in by_operation.items()},
        "by_day": {k: dict(v) for k, v in sorted(by_day.items())},
        "uncertain": uncertain,
        "log": str(log_path()),
    }


def paid_task_ids(source: str) -> list[Any]:
    """Return paid task IDs so callers can fetch results without paying again."""
    return [
        r["task_id"]
        for r in read_all()
        if r.get("source") == source and r.get("task_id") is not None
    ]
