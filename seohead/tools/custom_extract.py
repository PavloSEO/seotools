"""Custom extraction: pull arbitrary fields into columns, with a bounded budget.

A named extractor turns "does this exist" into "what is the actual value": the
price on a product page, the old phone number, the current build id in a
footer comment. Each becomes one column in the output and one field a
post-crawl pass can group pages by.

Three modes, in the order the interface should prefer them:
  css    -- a CSS selector (BeautifulSoup).
  xpath  -- an XPath 1.0 expression (lxml).
  regex  -- a regular expression run against the raw HTML string.

**A regular expression run against raw markup does not see what a browser
shows.** It matches bytes, not the DOM a script builds at runtime; a value
injected by JavaScript is invisible to it even though a reader's browser
renders it. CSS and XPath modes still only see the fetched document (rendered
DOM only if the caller supplies one — see the ``rendered`` flag below), but at
least they see *elements*, which is what most fields actually are. This
module surfaces the caveat in every regex result rather than only in this
docstring, because a report that repeats it once here and never again invites
someone to paste a regex-based finding without it.

**A pathological expression can stall a crawl.** ``re`` has no notion of a
step budget; the only mechanism that reliably reclaims control from a
catastrophic backtrack is a wall-clock deadline enforced by the OS, not by
Python bytecode the runaway match never returns to. Each (document,
extractor) pair therefore runs under ``timeout_seconds`` (default 2s),
enforced with ``SIGALRM`` where available (POSIX, main thread only). Hitting
it aborts *that document* for *that extractor*: its row is recorded with
``budget_exceeded: true`` and an empty value, execution moves to the next
document, and the whole run still finishes. On a platform or a caller thread
where ``SIGALRM`` cannot be installed, the budget cannot be enforced and every
document runs to completion instead — the caller is not lied to about which
duration each row honored; runs still complete, just without eviction of a
slow document.

The document contract matches ``custom_search``: ``{"url", "ok", "html",
"rendered"}``. Extraction only ever reads ``html`` — there is no rendered-text
shortcut here, since an extracted *value* (not a presence bit) needs the
actual markup to select or match against.

This module is pure and performs no network access.
"""

from __future__ import annotations

import re
import signal
from typing import Any

from bs4 import BeautifulSoup

MODES: tuple[str, ...] = ("css", "xpath", "regex")
OUTPUTS_BY_MODE: dict[str, tuple[str, ...]] = {
    "css": ("element", "html", "text"),
    "xpath": ("element", "html", "text"),
    "regex": ("text", "group"),
}
DEFAULT_TIMEOUT_SECONDS = 2.0


class _BudgetExceeded(Exception):
    pass


def _run_with_budget(func: Any, *args: Any, timeout_seconds: float) -> tuple[Any, bool]:
    """Run ``func(*args)`` under a wall-clock deadline. Returns ``(result, timed_out)``.

    Enforced with ``SIGALRM`` (POSIX, main thread only): CPython's regex engine
    does check for a pending signal during backtracking, which is what makes
    this actually interrupt a catastrophic-backtrack match rather than merely
    racing it. On a platform without ``SIGALRM``, or when called off the main
    thread (``signal.signal`` raises ``ValueError`` there), the deadline
    cannot be installed and ``func(*args)`` simply runs; ``timed_out`` is then
    always False rather than silently wrong.
    """
    if timeout_seconds <= 0 or not hasattr(signal, "SIGALRM"):
        return func(*args), False

    def _handler(signum: int, frame: Any) -> None:
        raise _BudgetExceeded()

    try:
        previous = signal.signal(signal.SIGALRM, _handler)
    except ValueError:
        return func(*args), False  # not the main thread; nothing to install onto
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return func(*args), False
    except _BudgetExceeded:
        return None, True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)  # cancel our alarm before restoring the handler
        signal.signal(signal.SIGALRM, previous)


def _outer_html(node: Any) -> str:
    return str(node)


def _inner_html(node: Any) -> str:
    return "".join(str(child) for child in getattr(node, "contents", []))


def _extract_css(html: str, selector: str, output: str) -> list[str]:
    soup = BeautifulSoup(html or "", features="lxml")
    matches = soup.select(selector)
    if output == "element":
        return [_outer_html(m) for m in matches]
    if output == "html":
        return [_inner_html(m) for m in matches]
    return [" ".join(m.get_text(" ").split()) for m in matches]


def _extract_xpath(html: str, expression: str, output: str) -> list[str]:
    from lxml import etree

    tree = etree.HTML((html or "").encode("utf-8", "ignore"))
    if tree is None:
        return []
    result = tree.xpath(expression)
    nodes = result if isinstance(result, list) else [result]
    values: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            values.append(node)  # text()/@attr already returns a string
        elif output == "element":
            values.append(etree.tostring(node, encoding="unicode"))
        elif output == "html":
            values.append("".join(etree.tostring(c, encoding="unicode") for c in node))
        else:
            values.append(" ".join((node.text_content() or "").split()))
    return values


def _extract_regex(
    html: str, pattern: str, output: str, group: int, case_sensitive: bool
) -> list[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = re.compile(pattern, flags)
    if output == "group":
        if group > compiled.groups:
            return []
        return [m.group(group) or "" for m in compiled.finditer(html or "") if m.group(group)]
    return [m.group(0) for m in compiled.finditer(html or "")]


def _extract_one(mode: str, html: str, spec: dict[str, Any]) -> list[str]:
    output = spec["output"]
    if mode == "css":
        return _extract_css(html, spec["query"], output)
    if mode == "xpath":
        return _extract_xpath(html, spec["query"], output)
    return _extract_regex(
        html,
        spec["query"],
        output,
        int(spec.get("group", 1)),
        bool(spec.get("case_sensitive", True)),
    )


def _representation(documents: list[dict[str, Any]]) -> Any:
    stamps = sorted({"rendered_dom" if d.get("rendered") else "static_markup" for d in documents})
    return stamps[0] if len(stamps) == 1 else stamps


def run_extractor(
    documents: list[dict[str, Any]],
    spec: dict[str, Any],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one extractor over ``documents``; see the module docstring for shapes."""
    mode = spec.get("mode", "css")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    output = spec.get("output", OUTPUTS_BY_MODE[mode][-1])
    if output not in OUTPUTS_BY_MODE[mode]:
        raise ValueError(
            f"output {output!r} is not valid for mode {mode!r}; expected one of {OUTPUTS_BY_MODE[mode]}"
        )
    if not spec.get("query"):
        raise ValueError("query is required")
    name = spec.get("name") or spec["query"]
    full_spec = {**spec, "output": output}

    fetched = [d for d in documents if d.get("ok", True)]
    rows = []
    aborted: list[str] = []
    for doc in fetched:
        html = doc.get("html") or ""
        values, timed_out = _run_with_budget(
            _extract_one, mode, html, full_spec, timeout_seconds=timeout_seconds
        )
        if timed_out:
            aborted.append(doc.get("url", ""))
            values = []
        rows.append(
            {
                "url": doc.get("url", ""),
                "values": values or [],
                "count": len(values or []),
                "budget_exceeded": timed_out,
            }
        )

    notes = []
    if mode == "regex":
        notes.append(
            "regex runs against raw HTML: a value injected by JavaScript is invisible to it; "
            "prefer css or xpath where the document supports it"
        )
    if aborted:
        notes.append(
            f"{len(aborted)} document(s) exceeded the {timeout_seconds}s budget for this "
            "extractor and were aborted; their rows carry no values"
        )

    return {
        "name": name,
        "mode": mode,
        "output": output,
        "query": spec["query"],
        "representation": _representation(fetched),
        "pages_considered": len(fetched),
        "pages_excluded_fetch_failed": len(documents) - len(fetched),
        "timeout_seconds": timeout_seconds,
        "rows": rows,
        "aborted_pages": aborted,
        "notes": notes,
    }


def run_extraction(
    documents: list[dict[str, Any]],
    extractors: list[dict[str, Any]],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run every extractor in ``extractors`` over ``documents``.

    Pure; no network access. A pathological extractor aborts only the
    document it stalled on (see :func:`run_extractor`) — this call always
    finishes.
    """
    return {
        "ok": True,
        "extractors": [
            run_extractor(documents, spec, timeout_seconds=timeout_seconds) for spec in extractors
        ],
    }
