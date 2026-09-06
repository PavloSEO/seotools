"""Diff two audits: what changed, not just what each one found separately.

The most repeated billable question in audit work is "did the developer
actually ship the fix", and a naive diff cannot answer it. A page that stopped
matching a finding because it was fixed, and a page that stopped matching
because it was deleted from the crawl entirely, look identical if you only
compare two sets of findings — and they mean opposite things.

So every finding lands in exactly one of four disjoint sets, keyed by the
finding's own fingerprint plus the URL it was found on:

    entered      the URL existed in both crawls; it did not match before, matches now
    left         the URL existed in both crawls; it matched before, does not match now
    appeared     the URL is new to this crawl, and matches now
    disappeared  the URL is gone from this crawl, and matched before

"left" is progress. "disappeared" is not progress — the URL that was broken is
simply no longer part of what was measured, which is a different fact and must
not be reported as a fix.

An audit-wide finding (no ``target_url`` — e.g. TITLE_TEMPLATED, which
describes the crawl as a whole) has no page to appear or disappear, so it can
only ever land in "entered" or "left": the condition it describes now holds,
or it no longer does.
"""

from __future__ import annotations

from typing import Any


class CompareError(ValueError):
    """Two audits that cannot be compared without lying about the result."""


def _key(issue: dict[str, Any]) -> tuple[str, str]:
    """(check, target_url) — the same finding on the same page, across runs.

    Not the fingerprint alone: the fingerprint already folds in target_url, so
    this is equivalent, but naming both parts keeps the four sets legible. An
    audit-wide issue has no target_url and keys on "" — that is never a real
    URL, so it cannot collide with a page-level finding of the same check.
    """
    return (issue.get("check", ""), str(issue.get("target_url") or ""))


def _crawled_urls(audit: dict[str, Any]) -> set[str]:
    return {p["url"] for p in audit.get("pages", []) if p.get("url")}


def _by_key(audit: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Every issue, keyed for diffing — including audit-wide ones (issue #213).

    Dropping issues with no target_url here made a real TITLE_TEMPLATED delta
    vanish from every bucket and the summary; compare must account for a
    finding it was actually given, not just the ones that name a page.
    """
    return {_key(issue): issue for issue in audit.get("issues", [])}


def preflight(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Reasons a comparison would mislead, without refusing outright.

    A caller decides whether to proceed; this only says what to distrust.
    """
    warnings: list[str] = []
    for label, audit in (("before", before), ("after", after)):
        if audit.get("run", {}).get("crawl_valid") is False:
            warnings.append(f"{label} crawl is marked invalid — it measured nothing usable")
    # A partial baseline and a partial current crawl poison opposite buckets, not
    # the same one (issue #212): a page the before crawl never reached looks
    # brand new to it, so "appeared" — not "disappeared" — is the bucket that
    # baseline can no longer prove; symmetrically, a page the after crawl never
    # reached looks gone to it, so "disappeared" is what that side poisons.
    if before.get("run", {}).get("crawl_partial"):
        warnings.append(
            "before crawl is partial — an 'appeared' finding may only mean the before "
            "crawl did not reach that URL, not that the URL is new"
        )
    if after.get("run", {}).get("crawl_partial"):
        warnings.append(
            "after crawl is partial — a 'disappeared' finding may only mean the after "
            "crawl did not reach that URL, not that the URL is gone"
        )
    before_cfg = before.get("run", {}).get("crawl_config")
    after_cfg = after.get("run", {}).get("crawl_config")
    # A missing crawl_config is an unknown comparison basis, not an established match --
    # the same distinction crawl_partial already draws above (issue #287). Only a native
    # crawl currently records this manifest, so an SF-derived audit reaches here with
    # none at all; silently treating "no manifest" as "same manifest" let a native-versus-
    # SF comparison classify same-URL finding changes as fixes or regressions when they
    # may just be two different tools measuring the same URL under different rules.
    if before_cfg is None or after_cfg is None:
        missing = [
            label for label, cfg in (("before", before_cfg), ("after", after_cfg)) if cfg is None
        ]
        warnings.append(
            f"{' and '.join(missing)} recorded no crawl configuration — comparability is "
            "unknown, not equal, so some of the difference may be the configuration rather "
            "than the site"
        )
    elif before_cfg != after_cfg:
        changed = sorted(
            k for k in set(before_cfg) | set(after_cfg) if before_cfg.get(k) != after_cfg.get(k)
        )
        warnings.append(
            "results-affecting settings differ between the two runs, so some of the "
            f"difference may be the configuration rather than the site: {', '.join(changed)}"
        )
    # crawl_config only exists for a native crawl; an SF-derived audit's nearest
    # equivalent today is its export profile (full/lite/...), which changes which
    # checks even had evidence to fire. Known profiles that differ are evidence of a
    # comparability gap even when neither side has a full effective manifest.
    before_profile = before.get("run", {}).get("profile")
    after_profile = after.get("run", {}).get("profile")
    if before_profile is not None and after_profile is not None and before_profile != after_profile:
        warnings.append(
            "SF export profile differs between the two runs "
            f"({before_profile!r} vs {after_profile!r}), so some of the difference may be "
            "the profile's check coverage rather than the site"
        )
    return warnings


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Diff two audit.json documents into the four sets, per check.

    Both documents must carry ``pages`` and ``issues`` in the shape this
    toolkit produces; a document missing either is refused by name rather than
    silently treated as empty, because an empty crawl and an unreadable one
    must not look the same in the result.
    """
    for label, audit in (("before", before), ("after", after)):
        if "pages" not in audit or "issues" not in audit:
            raise CompareError(f"{label} is not an audit.json document (missing pages or issues)")

    before_urls = _crawled_urls(before)
    after_urls = _crawled_urls(after)
    before_issues = _by_key(before)
    after_issues = _by_key(after)
    # A partial baseline cannot prove a URL it never reached is genuinely new —
    # only that it did not see it (issue #212). Without this, every finding on
    # a URL outside the truncated baseline is misreported as "appeared".
    before_partial = bool(before.get("run", {}).get("crawl_partial"))
    # Symmetrically, a partial after-crawl cannot prove a URL it never reached
    # is genuinely gone — only that it did not see it (issue #458). Without
    # this, every finding on a URL outside the truncated after crawl is
    # misreported as "disappeared" instead of the unproven "left".
    after_partial = bool(after.get("run", {}).get("crawl_partial"))

    entered: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    appeared: list[dict[str, Any]] = []
    disappeared: list[dict[str, Any]] = []

    all_keys = set(before_issues) | set(after_issues)
    for key in all_keys:
        url = key[1]
        in_before = key in before_issues
        in_after = key in after_issues

        if in_before and in_after:
            continue  # unchanged: matched in both, not a difference

        # An audit-wide finding (no target_url, key[1] == "") describes the
        # crawl as a whole rather than a page, so it has no page-presence to
        # test and can only enter or leave (issue #213) — never appear or
        # disappear, which both assert something about a URL's existence.
        if not url:
            if in_after and not in_before:
                entered.append(dict(after_issues[key]))
            else:
                left.append(dict(before_issues[key]))
            continue

        url_in_before_crawl = url in before_urls
        url_in_after_crawl = url in after_urls

        if in_after and not in_before:
            record = dict(after_issues[key])
            if url_in_before_crawl or before_partial:
                entered.append(record)  # existed before, is a new finding now
            else:
                appeared.append(record)  # the URL itself is new to this crawl
        elif in_before and not in_after:
            record = dict(before_issues[key])
            if url_in_after_crawl or after_partial:
                left.append(record)  # still crawled, no longer matches — a fix
            else:
                disappeared.append(record)  # not in this crawl at all — unproven

    def _sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda i: (i.get("check", ""), str(i.get("target_url") or "")))

    by_check: dict[str, dict[str, int]] = {}
    for bucket_name, bucket in (
        ("entered", entered),
        ("left", left),
        ("appeared", appeared),
        ("disappeared", disappeared),
    ):
        for item in bucket:
            row = by_check.setdefault(
                item["check"], {"entered": 0, "left": 0, "appeared": 0, "disappeared": 0}
            )
            row[bucket_name] += 1

    return {
        "schema_version": "compare.v1",
        "before": {
            "generated_at": before.get("run", {}).get("generated_at"),
            "urls_crawled": len(before_urls),
        },
        "after": {
            "generated_at": after.get("run", {}).get("generated_at"),
            "urls_crawled": len(after_urls),
        },
        "warnings": preflight(before, after),
        "summary": {
            "entered": len(entered),
            "left": len(left),
            "appeared": len(appeared),
            "disappeared": len(disappeared),
            "by_check": by_check,
        },
        "entered": _sort(entered),
        "left": _sort(left),
        "appeared": _sort(appeared),
        "disappeared": _sort(disappeared),
    }
