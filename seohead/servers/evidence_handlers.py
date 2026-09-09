"""Read-only projections and explicit saved-scan mutation boundaries."""

from __future__ import annotations

import json
from typing import Any


def scan_evidence(input_path: str, section: str = "capabilities", limit: int = 1000, offset: int = 0) -> dict[str, Any]:
    from seohead.storage import open_scan
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 10000 or offset < 0:
        raise ValueError("limit must be 1..10000 and offset nonnegative")
    if section not in {"capabilities", "corpus", "structured", "routes", "resources", "timeline"}:
        raise ValueError("unknown saved evidence section")
    con = open_scan(input_path, require_audit=False)
    try:
        if section == "resources":
            from seohead.storage.resource_graph import read
            result = read(con, limit=limit, offset=offset)
        elif section == "timeline":
            from seohead.storage.events import timeline
            result = timeline(con, limit=limit)
        elif section == "routes":
            from seohead.storage.rendered_routes import read
            result = read(con)
        elif section == "corpus":
            from seohead.storage.content_evidence import read
            result = read(con)
        elif section == "structured":
            from seohead.storage.structured_evidence import read
            result = read(con)
        else:
            row = con.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()
            if row is None:
                result = {"state": "unavailable", "reason": "no saved audit capability projection"}
            else:
                audit = json.loads(row[0])
                from seohead.sf.core.evidence_contract import attach_contract
                identity = con.execute("SELECT scan_uuid FROM scan WHERE singleton=1").fetchone()[0]
                result = attach_contract(audit, scan_uuid=identity, con=con)["summary"]["evidence_contract"]
        return {"ok": True, "section": section, "evidence": result}
    finally:
        con.close()


def scan_extract(input_path: str, rules: list[dict[str, Any]], url: str | None = None, representation: str = "static", limit: int = 100) -> dict[str, Any]:
    """Apply bounded declarative rules to retained bodies without a fetch or mutation."""
    from seohead.storage import open_scan
    from seohead.storage.bodies import read_document
    from seohead.storage import ScanError
    from seohead.tools.extraction_rules import evaluate, validate_rules
    from seohead.tools.parser import parse_html
    if representation not in {"static", "rendered", "legacy_fragment"} or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid representation or page limit")
    selected = validate_rules(rules)
    con = open_scan(input_path, require_audit=False)
    try:
        where = "d.representation=?"
        parameters: list[Any] = [representation]
        if url is not None:
            where += " AND u.url=?"
            parameters.append(url)
        rows = con.execute("SELECT d.*,u.url FROM documents d JOIN urls u USING(url_id) WHERE " + where + " ORDER BY document_id LIMIT ?", (*parameters, limit + 1)).fetchall()
        items = []
        output_bytes = 0
        output_limited = False
        for row in rows[:limit]:
            html = None
            reason = ""
            try:
                html = read_document(con, row["document_id"], max_decoded_bytes=8 * 1024 * 1024)
            except ScanError as exc:
                reason = str(exc)
            parsed = parse_html(html, row["url"]) if html is not None else None
            result = evaluate(html=html, parsed=parsed, rules=selected, representation=representation)
            if reason:
                result["reason"] = reason
            item = {"url": row["url"], "document_id": row["document_id"], "representation": representation, "result": result}
            size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if output_bytes + size > 8 * 1024 * 1024:
                output_limited = True
                break
            items.append(item)
            output_bytes += size
        return {"ok": True, "items": items, "truncated": len(rows) > limit or output_limited, "output_limit_bytes": 8 * 1024 * 1024, "scope": "retained complete bodies only; no network or artifact mutation"}
    finally:
        con.close()
