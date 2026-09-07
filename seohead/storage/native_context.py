"""Closed versioned native context; observations remain data, never instructions."""

import json
import math
from typing import Any

from . import ScanError, _insert


def validate_context(
    con: Any, item: dict[str, Any], *, sitemap_roots: set[int] | None = None
) -> None:
    if set(item) != {
        "kind",
        "item_key",
        "payload_version",
        "payload_json",
        "completeness",
        "reason",
    }:
        raise ScanError("native context has unknown or missing fields")
    if any(type(item[key]) is not str for key in item):
        raise ScanError("native context metadata must be strings")
    if item["completeness"] not in {"complete", "partial", "unavailable"}:
        raise ScanError("native context completeness is invalid")
    if item["payload_version"] != "scan_context.v1":
        raise ScanError("native scan context payload version is invalid")
    try:
        payload = json.loads(item["payload_json"])
    except (TypeError, ValueError) as exc:
        raise ScanError("native scan context payload is invalid JSON") from exc
    from . import sitemaps

    if item["kind"] in sitemaps.KINDS:
        sitemaps.validate_context(con, item, payload, sitemap_roots)
        return
    if item["kind"] == "resource_inventory":
        from .resources import validate_inventory_context

        validate_inventory_context(con, item)
        return
    if item["kind"] == "resource_commit":
        if (
            not isinstance(payload, dict)
            or set(payload) != {"digest", "requests_used"}
            or type(payload["digest"]) is not str
            or len(payload["digest"]) != 64
            or any(ch not in "0123456789abcdef" for ch in payload["digest"])
            or type(payload["requests_used"]) is not int
            or payload["requests_used"] < 0
            or item["completeness"] != "complete"
            or item["reason"]
            or not item["item_key"].startswith("resource:")
            or not item["item_key"][9:].isascii()
            or not item["item_key"][9:].isdigit()
        ):
            raise ScanError("native resource commit context is invalid")
        return
    if item["kind"] == "reanalysis_provenance":
        if (
            item["item_key"] != "run"
            or item["completeness"] != "complete"
            or item["reason"] != "offline reanalysis"
            or not isinstance(payload, dict)
            or set(payload)
            != {
                "parent_scan_uuid",
                "capture_scan_uuid",
                "source_evidence_revision",
                "derived_evidence_revision",
                "source_audit_sha256",
                "source_writer_version",
                "source_writer_revision",
                "source_runtime_versions_json",
                "source_config_sha256",
                "capture_writer_version",
                "capture_writer_revision",
                "capture_runtime_versions_json",
                "capture_config_sha256",
                "capture_run",
            }
            or not isinstance(payload["parent_scan_uuid"], str)
            or not isinstance(payload["capture_scan_uuid"], str)
            or type(payload["source_evidence_revision"]) is not int
            or payload["source_evidence_revision"] < 0
            or type(payload["derived_evidence_revision"]) is not int
            or payload["derived_evidence_revision"] != payload["source_evidence_revision"] + 1
            or (
                payload["source_audit_sha256"] is not None
                and (
                    not isinstance(payload["source_audit_sha256"], str)
                    or len(payload["source_audit_sha256"]) != 64
                    or any(ch not in "0123456789abcdef" for ch in payload["source_audit_sha256"])
                )
            )
            or not isinstance(payload["source_writer_version"], str)
            or not isinstance(payload["source_writer_revision"], str)
            or len(payload["source_writer_revision"]) != 40
            or any(ch not in "0123456789abcdef" for ch in payload["source_writer_revision"])
            or not isinstance(payload["source_runtime_versions_json"], str)
            or not isinstance(payload["source_config_sha256"], str)
            or len(payload["source_config_sha256"]) != 64
            or any(ch not in "0123456789abcdef" for ch in payload["source_config_sha256"])
            or not isinstance(payload["capture_writer_version"], str)
            or not isinstance(payload["capture_writer_revision"], str)
            or len(payload["capture_writer_revision"]) != 40
            or any(ch not in "0123456789abcdef" for ch in payload["capture_writer_revision"])
            or not isinstance(payload["capture_runtime_versions_json"], str)
            or not isinstance(payload["capture_config_sha256"], str)
            or len(payload["capture_config_sha256"]) != 64
            or any(ch not in "0123456789abcdef" for ch in payload["capture_config_sha256"])
        ):
            raise ScanError("native reanalysis provenance is invalid")
        capture = payload["capture_run"]
        if (
            not isinstance(capture, dict)
            or set(capture)
            != {"lifecycle", "finish_reason", "crawl_partial", "created_at", "finished_at"}
            or capture["lifecycle"] not in {"running", "interrupted", "finished", "failed"}
            or not isinstance(capture["finish_reason"], str)
            or type(capture["crawl_partial"]) is not int
            or capture["crawl_partial"] not in (0, 1)
        ):
            raise ScanError("reanalysis capture state is invalid")
        from .corpus_validation import _timestamp

        _timestamp(capture["created_at"], "capture created_at")
        _timestamp(capture["finished_at"], "capture finished_at", nullable=True)
        return
    if item["kind"] == "credential_context":
        verifier = payload.get("verifier") if isinstance(payload, dict) else object()
        if (
            item["item_key"] != "run"
            or item["completeness"] != "complete"
            or item["reason"]
            or not isinstance(payload, dict)
            or set(payload) != {"verifier", "implicit_state"}
            or (verifier is not None and not isinstance(verifier, str))
            or (
                isinstance(verifier, str)
                and (
                    len(verifier) != 64
                    or any(character not in "0123456789abcdef" for character in verifier)
                )
            )
            or type(payload["implicit_state"]) is not bool
        ):
            raise ScanError("native credential context is invalid")
        return
    if item["kind"] == "native_commit":
        if (
            not item["item_key"].isascii()
            or not item["item_key"].isdigit()
            or str(int(item["item_key"])) != item["item_key"]
            or not isinstance(payload, dict)
            or set(payload) != {"digest"}
            or not isinstance(payload["digest"], str)
            or len(payload["digest"]) != 64
            or any(char not in "0123456789abcdef" for char in payload["digest"])
            or item["completeness"] != "complete"
            or item["reason"] != "atomic page commit"
        ):
            raise ScanError("native scan commit idempotency context is invalid")
        if not con.execute(
            "SELECT 1 FROM frontier WHERE queue_ordinal=? AND state='done'",
            (int(item["item_key"]),),
        ).fetchone():
            raise ScanError("native scan commit context does not name a completed lease")
        return
    if item["kind"] == "seed_url":
        if (
            not isinstance(payload, dict)
            or set(payload) != {"url_id", "depth", "source"}
            or type(payload["url_id"]) is not int
            or payload["depth"] != 0
            or payload["source"] != "sitemap"
            or item["item_key"] != f"url:{payload['url_id']}"
        ):
            raise ScanError("native seed context is invalid")
        if not con.execute(
            "SELECT 1 FROM frontier WHERE url_id=? AND depth=0 AND state!='excluded'",
            (payload["url_id"],),
        ).fetchone():
            raise ScanError("native seed context must reference an accepted seed")
        return
    if item["kind"] == "robots_summary":
        if (
            item["item_key"] != "run"
            or not isinstance(payload, dict)
            or set(payload)
            != {"policy", "token", "fetch_state", "final_response_id", "note", "parsed"}
        ):
            raise ScanError("native robots summary shape is invalid")
        if (
            payload["policy"] not in {"respect", "report_only", "ignore"}
            or type(payload["token"]) is not str
            or type(payload["note"]) is not str
            or payload["fetch_state"] not in {"fetched", "unavailable", "not_fetched"}
            or payload["final_response_id"] is not None
        ):
            raise ScanError("native robots summary provenance is invalid")
        parsed = payload["parsed"]
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"groups", "sitemaps"}
            or not isinstance(parsed["groups"], list)
            or not isinstance(parsed["sitemaps"], list)
            or any(type(value) is not str for value in parsed["sitemaps"])
        ):
            raise ScanError("native parsed robots summary is invalid")
        if payload["fetch_state"] != "fetched" and parsed != {"groups": [], "sitemaps": []}:
            raise ScanError("unavailable robots summary cannot invent parsed rules")
        for group in parsed["groups"]:
            legacy_keys = {
                "user_agents",
                "allow",
                "disallow",
                "crawl_delay",
            }
            current_keys = legacy_keys | {"request_rate_delay"}
            if not isinstance(group, dict) or set(group) not in (legacy_keys, current_keys):
                raise ScanError("native robots group shape is invalid")
            if any(
                not isinstance(group[key], list)
                or any(type(value) is not str for value in group[key])
                for key in ("user_agents", "allow", "disallow")
            ):
                raise ScanError("native robots rules must be string lists")
            delay = group["crawl_delay"]
            if delay is not None and (
                type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0
            ):
                raise ScanError("native robots delay must be finite and nonnegative")
            rate_delay = group.get("request_rate_delay")
            if rate_delay is not None and (
                type(rate_delay) not in (int, float)
                or not math.isfinite(rate_delay)
                or rate_delay <= 0
            ):
                raise ScanError("native robots request-rate delay must be finite and positive")
        return
    if item["kind"] != "robots_blocked_url":
        raise ScanError(
            "native context kind is unsupported; expected native_commit, "
            "robots_blocked_url, robots_summary or seed_url"
        )
    if (
        not isinstance(payload, dict)
        or set(payload) != {"url_id", "token", "policy"}
        or type(payload["url_id"]) is not int
        or payload["url_id"] <= 0
        or item["item_key"] != f"url:{payload['url_id']}"
        or type(payload["token"]) is not str
        or payload["policy"] not in {"respect", "report_only"}
    ):
        raise ScanError("native scan robots_blocked_url context is invalid")
    url = con.execute("SELECT url FROM urls WHERE url_id=?", (payload["url_id"],)).fetchone()
    if url is None:
        raise ScanError("native scan robots context references an unknown URL")
    if (
        payload["policy"] == "respect"
        and not con.execute(
            "SELECT 1 FROM decisions WHERE url=? AND reason='blocked_by_robots'", (url[0],)
        ).fetchone()
    ):
        raise ScanError("native robots exclusion lacks its blocked_by_robots decision")


def put_context(con: Any, item: dict[str, Any], *, sitemap_roots: set[int] | None = None) -> None:
    validate_context(con, item, sitemap_roots=sitemap_roots)
    existing = con.execute(
        "SELECT * FROM context_items WHERE kind=? AND item_key=?", (item["kind"], item["item_key"])
    ).fetchone()
    if existing is not None:
        if dict(existing) != item:
            raise ScanError("native context retry disagrees with committed observation")
        return
    _insert(con, "context_items", item)
