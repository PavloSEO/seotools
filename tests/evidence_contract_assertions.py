"""Assertions for additive saved-audit evidence, separate from semantic verdict parity."""

from __future__ import annotations

import copy
from typing import Any

from seohead.sf.core.evidence_contract import SAVED_CORPUS_VERSION, stable_evidence_id


def semantic_audit(document: dict[str, Any]) -> dict[str, Any]:
    """Remove only scan-local additive references before comparing verdicts."""
    result = copy.deepcopy(document)
    summary = result.get("summary")
    if isinstance(summary, dict):
        summary.pop("evidence_contract", None)
        summary.pop("saved_corpus_derivations", None)
    for issue in result.get("issues", []):
        if not isinstance(issue, dict) or not isinstance(issue.get("evidence"), dict):
            continue
        issue["evidence"].pop("contract", None)
        if not issue["evidence"]:
            issue.pop("evidence")
    return result


def assert_saved_contract(document: dict[str, Any], con: Any) -> None:
    """Require every emitted reference to resolve in this artifact's own scan."""
    scan_uuid = con.execute("SELECT scan_uuid FROM scan WHERE singleton=1").fetchone()[0]
    summary = document["summary"]
    envelope = summary["evidence_contract"]
    assert envelope["scan_identity_state"] == "measured"
    assert envelope["scan_uuid"] == scan_uuid
    assert envelope["population"]["urls_crawled"] == con.execute(
        "SELECT COUNT(*) FROM pages"
    ).fetchone()[0]
    for issue in document["issues"]:
        contract = issue["evidence"]["contract"]
        finding = contract["finding"]
        _assert_reference(finding, scan_uuid, con, issue_id=issue["id"])
        for reference in contract["observations"]:
            _assert_reference(reference, scan_uuid, con)

    derivations = summary["saved_corpus_derivations"]
    assert derivations["schema_version"] == SAVED_CORPUS_VERSION
    for declaration in derivations["internationalization"]["declarations"]:
        target = declaration["target_observation"]
        if target["state"] != "observed":
            continue
        row = con.execute("SELECT document_id FROM pages WHERE url_id=?", (target["page_url_id"],)).fetchone()
        assert row is not None
        if target["document_id"] is not None:
            assert row[0] == target["document_id"]


def _assert_reference(reference: dict[str, Any], scan_uuid: str, con: Any, *, issue_id: str | None = None) -> None:
    assert reference["state"] == "measured"
    assert reference["scan_uuid"] == scan_uuid
    assert reference["id"] == stable_evidence_id(
        scan_uuid=scan_uuid,
        source_table=reference["source_table"],
        observation_id=reference["observation_id"],
    )
    table, observation_id = reference["source_table"], reference["observation_id"]
    if table == "audit":
        assert observation_id == f"issue:{issue_id}"
    elif table in {"pages", "urls"}:
        identifier = _number(observation_id, "url_id:")
        assert con.execute(f"SELECT 1 FROM {table} WHERE url_id=?", (identifier,)).fetchone()
    elif table == "documents":
        identifier = _number(observation_id, "document_id:")
        assert con.execute("SELECT 1 FROM documents WHERE document_id=?", (identifier,)).fetchone()
    elif table == "responses":
        identifier = _number(observation_id, "response_id:")
        assert con.execute("SELECT 1 FROM responses WHERE response_id=?", (identifier,)).fetchone()
    elif table == "context_items":
        marker = ":ordinal:" if ":ordinal:" in observation_id else ":state:"
        prefix = "language_evidence:"
        assert observation_id.startswith(prefix) and marker in observation_id
        item_key = observation_id[len(prefix) :].rsplit(marker, 1)[0]
        assert con.execute(
            "SELECT 1 FROM context_items WHERE kind='language_evidence' AND item_key=?",
            (item_key,),
        ).fetchone()
    else:
        raise AssertionError(f"unsupported saved evidence table: {table}")


def _number(value: str, prefix: str) -> int:
    assert value.startswith(prefix)
    return int(value[len(prefix) :])
