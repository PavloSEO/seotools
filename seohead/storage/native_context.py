"""Closed versioned native context; observations remain data, never instructions."""

import json
import math

from . import ScanError, _insert


def validate_context(con, item):
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
            if not isinstance(group, dict) or set(group) != {
                "user_agents",
                "allow",
                "disallow",
                "crawl_delay",
            }:
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


def put_context(con, item):
    validate_context(con, item)
    existing = con.execute(
        "SELECT * FROM context_items WHERE kind=? AND item_key=?", (item["kind"], item["item_key"])
    ).fetchone()
    if existing is not None:
        if dict(existing) != item:
            raise ScanError("native context retry disagrees with committed observation")
        return
    _insert(con, "context_items", item)
