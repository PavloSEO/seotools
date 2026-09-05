"""Issue aggregation: stable ids, dedup, page back-links and the summary block."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from .context import AuditContext
from .models import AuditResult, Issue, SkippedCheck


def _fingerprint(issue: Issue) -> str:
    """Return a deterministic hash for diffing audit runs.

    Keyed on (check, target_url, status) only — NOT on the locations list, which
    is capped/ordered and would make the fingerprint unstable across runs.
    """
    basis = "|".join([issue.check, str(issue.target_url), str(issue.status_code)])
    return hashlib.sha1(basis.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen: dict[tuple[str, str | None], Issue] = {}
    out: list[Issue] = []
    for issue in issues:
        key = (issue.check, issue.target_url)
        if key in seen and issue.target_url is not None:
            # merge locations into the first occurrence rather than duplicate
            existing = seen[key]
            existing.locations.extend(issue.locations)
            unique_sources = {
                loc.get("source_url") for loc in existing.locations if loc.get("source_url")
            }
            # never undercount: locations may be capped, so keep the largest signal
            existing.occurrences_count = max(
                len(unique_sources), existing.occurrences_count, issue.occurrences_count
            )
            continue
        seen[key] = issue
        out.append(issue)
    return out


# Crawling far fewer URLs than the sitemap declares is normal — a deliberate
# sample, a URL limit — so it marks the run partial rather than invalid. Scoring
# a fraction of a site as if it were the whole one is still worth flagging.
PARTIAL_CRAWL_RATIO = 0.2

# Below this share of the registry the score stops meaning anything: a source
# serving a fifth of the checks would grade a site on almost no evidence.
MIN_COVERAGE_TO_SCORE = 0.5

# Every one of these findings asserts a negative over the whole site — "no
# hyperlink reaches this", "nothing links to it" — which is only provable once
# every URL has been fetched. On a partial crawl the missing link may simply be
# in the part that was never crawled, so proving the negative is unsound and
# the finding is withheld rather than footnoted (issue #15's design
# requirement: "partial crawls poison specific conclusions").
UNLINKED_FINDING_CHECKS = frozenset(
    {
        "ORPHAN_PAGE",
        "SITEMAP_ORPHAN",
        "UNLINKED_CANONICAL",
        "UNLINKED_PAGINATION_SERIES",
    }
)


def _withhold_unlinked_findings(ctx: AuditContext, issues: list[Issue]) -> list[Issue]:
    """Drop findings that assert "nothing links here" and declare why.

    Each withheld check moves from a finding to a named skip, the same
    discipline ``ctx.skip`` already applies to missing evidence: a check that
    could not be proven must not look identical to a check that ran clean.
    """
    withheld = {i.check for i in issues if i.check in UNLINKED_FINDING_CHECKS}
    if not withheld:
        return issues
    for check_id in sorted(withheld):
        # retract, not skip: the check has already fired, and skip refuses a
        # fired check by design. Withholding is the one legitimate move from
        # fired to skipped, and it has to be a single operation or the issues
        # are dropped while the skip is silently refused -- the check vanishing
        # from both buckets, which is exactly what this branch fixes in #136.
        ctx.retract(
            check_id,
            "crawl is partial: an 'unlinked' finding cannot be proven when the "
            "crawl did not reach every URL",
        )
    return [i for i in issues if i.check not in UNLINKED_FINDING_CHECKS]


# Each of these judges the *whole* observed edge set at once -- an iterative
# score over every inlink, "the only inlinks are X", the shortest click path
# from the seed -- rather than a single edge in isolation. On a native crawl
# stopped early (a URL limit, a duration limit, an error streak, an
# interruption) the unfetched frontier can still hold the very edges that
# would change the verdict, so the same #15 "partial crawls poison specific
# conclusions" reasoning applies here even though none of these four assert
# "nothing links here" the way UNLINKED_FINDING_CHECKS does (issue #246).
# GENERIC_ANCHOR_TEXT and friends stay eligible: each fires on one observed
# edge that is already proof of itself, regardless of what the rest of the
# graph turns out to contain.
GRAPH_WIDE_FINDING_CHECKS = frozenset(
    {
        "LOW_LINK_SCORE",
        "ONLY_NOFOLLOW_INLINKS",
        "ONLY_NONINDEXABLE_SOURCE_INLINKS",
        "DEEP_DISCOVERY_PATH",
    }
)


def _withhold_graph_wide_findings(ctx: AuditContext, issues: list[Issue]) -> list[Issue]:
    """Drop findings that judge the whole link graph and declare why.

    Same shape as ``_withhold_unlinked_findings`` -- a distinct check set,
    because these are whole-graph conclusions rather than "unlinked"
    assertions, but withheld for the identical reason: the missing frontier
    of a partial native crawl could still hold the edges the verdict depends
    on.
    """
    withheld = {i.check for i in issues if i.check in GRAPH_WIDE_FINDING_CHECKS}
    if not withheld:
        return issues
    for check_id in sorted(withheld):
        # retract, not skip: these checks already fired (they judged the
        # observed edges and found an outlier), and ctx.skip() no-ops on a
        # check_id already in _fired_ids by design -- the same reason
        # _withhold_unlinked_findings above uses retract instead of skip.
        # Calling skip() here let the finding vanish from ctx.issues (via the
        # list comprehension below) while never landing in ctx.skipped either,
        # so the check read as "silent" (never invoked) instead of "withheld
        # with a reason" -- indistinguishable from a check nobody ran, which
        # is exactly the confusion this module exists to prevent (issue #246).
        ctx.retract(
            check_id,
            "crawl is partial: a whole-graph finding cannot be proven when the "
            "crawl did not reach every URL",
        )
    return [i for i in issues if i.check not in GRAPH_WIDE_FINDING_CHECKS]


def _crawl_validity(
    n_pages: int, by_check: dict[str, int], urls_crawled: int
) -> tuple[bool, str | None]:
    """Decide whether the run produced a corpus worth scoring.

    A score of 100 next to a critical NO_RESPONSE is a false green: the run
    proved nothing, and that number is the one that reaches a client report.
    """
    if urls_crawled <= 0:
        return False, "no URLs were crawled"
    if n_pages <= 0:
        reason = "no HTML pages were crawled"
        if by_check.get("NO_RESPONSE"):
            reason = "the crawl got no response from the site"
        return False, reason
    return True, None


def _health_score(
    by_severity: dict[str, int], n_pages: int, weights: dict[str, float]
) -> int | None:
    """Score the crawl, or return ``None`` when there is nothing to score.

    Returning 100 for an empty corpus was arithmetically defensible and
    completely misleading: it is the one place the toolkit rendered "no data"
    as "no problems".
    """
    if n_pages <= 0:
        return None
    penalty = sum(by_severity.get(sev, 0) * w for sev, w in weights.items())
    score = 100 - (penalty / n_pages) * 10
    return max(0, min(100, round(score)))


# A check that fires on most of a crawl is almost always wrong. This tool exists
# to find the unusual, so a finding that describes the majority of a site is
# either a site-wide fact stated one page at a time, or a defect in the check --
# and the three defects found on live sites in issue #98 were all the second.
# #94 accounted for 74% of one report before anybody noticed by reading it.
#
# Not a failure: a site really can have no meta descriptions anywhere, and saying
# so 400 times is correct. This only says which checks a reviewer must look at
# before trusting the report, which is the one line that would have caught all
# three.
IMPLAUSIBLE_SHARE = 0.5


def _implausible_checks(issues: list[Issue], n_pages: int) -> list[dict[str, Any]]:
    """Checks whose findings cover more than ``IMPLAUSIBLE_SHARE`` of the crawled pages.

    Counted by distinct page, not by occurrence: a check can fire many times on
    one page (#94 fired 392 times across 124 pages), and it is the breadth that
    makes a finding suspect, not the volume.
    """
    if n_pages <= 0:
        return []
    pages_by_check: dict[str, set[str]] = {}
    for issue in issues:
        targets = {issue.target_url} if issue.target_url else set()
        targets |= {
            str(loc.get("url"))
            for loc in issue.locations
            if isinstance(loc, dict) and loc.get("url")
        }
        if targets:
            pages_by_check.setdefault(issue.check, set()).update(targets)
    flagged = [
        {
            "check": check_id,
            "pages": len(urls),
            "share": round(len(urls) / n_pages, 3),
        }
        for check_id, urls in pages_by_check.items()
        if len(urls) / n_pages > IMPLAUSIBLE_SHARE
    ]
    return sorted(flagged, key=lambda row: (-row["share"], row["check"]))


def aggregate(
    ctx: AuditContext,
    run: dict[str, Any],
    size_stats: dict[str, Any],
    sitemap_summary: dict[str, Any],
) -> AuditResult:
    issues = _dedupe(ctx.issues)

    # Partial-crawl status must be known before issues are finalized: an
    # "unlinked"/"orphan" finding computed on a truncated crawl is unproven,
    # and withholding it after ids/back-links are assigned would leave stale
    # references. Both crawl_valid and crawl_partial depend only on counts
    # that do not themselves change when unlinked findings are withheld
    # (NO_RESPONSE presence; total URLs crawled), so they are safe to compute
    # up front.
    urls_crawled = len(ctx.pages)
    n_pages = len(ctx.html_pages())
    crawl_valid, invalid_reason = _crawl_validity(
        n_pages, dict(Counter(i.check for i in issues)), urls_crawled
    )
    urls_in_sitemap = int((sitemap_summary or {}).get("urls_in_sitemap") or 0)
    sitemap_partial = bool(
        urls_in_sitemap and crawl_valid and urls_crawled < urls_in_sitemap * PARTIAL_CRAWL_RATIO
    )
    # A caller may already know the run is partial — a URL limit, a timeout, an
    # interrupted crawl — before any sitemap comparison happens. That signal
    # must survive here, not be replaced by a sitemap check that has nothing to
    # say when there is no sitemap.
    crawl_partial = bool(run.get("crawl_partial")) or sitemap_partial
    if crawl_partial:
        issues = _withhold_unlinked_findings(ctx, issues)
        issues = _withhold_graph_wide_findings(ctx, issues)

    # assign ordered ids + fingerprints (sorted for determinism)
    sev_rank = {"critical": 0, "warning": 1, "notice": 2}
    issues.sort(key=lambda i: (sev_rank.get(i.severity, 3), i.check, str(i.target_url)))
    for n, issue in enumerate(issues, start=1):
        issue.id = f"ISSUE-{n:06d}"
        issue.fingerprint = _fingerprint(issue)

    # back-link issues onto pages
    for issue in issues:
        page = ctx.page_by_url.get(issue.target_url) if issue.target_url else None
        if page is not None:
            if issue.check not in page.issues:
                page.issues.append(issue.check)
            page.issue_ids.append(issue.id)

    # strip private record from page metrics before serialization
    for page in ctx.pages:
        page.metrics.pop("_record", None)

    by_severity = Counter(i.severity for i in issues)
    by_check = Counter(i.check for i in issues)
    weights = ctx.config.get("scoring", {}).get("weights", {})

    summary: dict[str, Any] = {
        "totals": {
            "urls_crawled": len(ctx.pages),
            "html_pages": n_pages,
            "html_indexable": len(ctx.indexable_html_pages()),
            "issues_total": len(issues),
            "groups_total": len(ctx.groups),
            # "static" unless a collector recorded otherwise (#18). Kept in
            # the standard totals block, not a rendering-specific one, so
            # every report -- crawled natively or loaded from an SF export --
            # states the two populations without a caller having to ask.
            "pages_by_representation": dict(
                sorted(
                    Counter(
                        (page.metrics.get("representation") or "static") for page in ctx.pages
                    ).items()
                )
            ),
        },
        "by_severity": {
            "critical": by_severity.get("critical", 0),
            "warning": by_severity.get("warning", 0),
            "notice": by_severity.get("notice", 0),
        },
        "by_check": dict(sorted(by_check.items(), key=lambda kv: (-kv[1], kv[0]))),
        # Checks a reviewer must look at before trusting the rest (issue #98).
        # Empty is the ordinary case, and an empty list is still reported so
        # "nothing looked suspicious" is visible rather than inferred from a
        # missing key.
        "implausible_checks": _implausible_checks(issues, n_pages),
        "health_score": _health_score(by_severity, n_pages, weights),
    }

    run["crawl_valid"] = crawl_valid
    run["crawl_invalid_reason"] = invalid_reason
    if not crawl_valid:
        summary["health_score"] = None
        summary["health_score_reason"] = invalid_reason

    # An empty SPA shell or a start page with zero internal links fetches
    # fine and produces a clean-looking, one-page audit -- the false-green
    # #18's gate exists to catch. The collector (seohead.crawl, via
    # seohead.servers.handlers) decides this and passes it through ``run``,
    # the same channel crawl_partial already uses, so seohead.sf never has
    # to import the collector to honour its verdict.
    requires_rendering = bool(run.get("requires_rendering"))
    run["requires_rendering"] = requires_rendering
    if requires_rendering and summary["health_score"] is not None:
        summary["health_score"] = None
        summary["health_score_reason"] = run.get("requires_rendering_reason") or (
            "the run requires JavaScript rendering before it can be scored"
        )

    # A score built from half the checks is not comparable to one built from all
    # of them, and the difference is invisible in the number. Fewer checks means
    # less penalty means a HIGHER score, so silence here rewards missing data.
    from seohead.sf.core.registry import CHECKS

    checks_total = len(CHECKS)
    # Four disjoint buckets partition the registry, all derived here in one place
    # (issue #177) so the summary counts and the returned records can never
    # disagree: `ctx.add` already refuses to record an issue for a disabled
    # check, so fired and disabled cannot overlap by construction; a check that
    # both fired and separately declared a skip (issue #136 — one source found
    # evidence, another didn't) counts only as fired, computed once instead of
    # twice as aggregate.py used to (checks_skipped counted the raw declaration,
    # the returned list subtracted fired — the two could disagree).
    fired_ids = {i.check for i in issues}
    # An operator's own config switch must never read as a clean/silent result
    # (issue #177): `enabled()` is config-only, so this is knowable independent
    # of whether any code path actually evaluated the check.
    disabled_ids = {check_id for check_id in CHECKS if not ctx.enabled(check_id)}
    declared_ids = {s.id for s in ctx.skipped} - fired_ids - disabled_ids
    # "Silent" now means only "invoked and found nothing" (issue #177): a check
    # that was never invoked at all is a defect, caught by
    # tests/chains/test_crawl_check_coverage.py, not absorbed quietly here.
    silent_ids = set(CHECKS) - fired_ids - disabled_ids - declared_ids

    checks_skipped = len(declared_ids)
    checks_disabled = len(disabled_ids)
    checks_available = checks_total - checks_skipped - checks_disabled
    summary["check_coverage"] = {
        "checks_total": checks_total,
        "checks_fired": len(fired_ids),
        "checks_skipped": checks_skipped,
        "checks_disabled": checks_disabled,
        "checks_disabled_ids": sorted(disabled_ids),
        "checks_silent": len(silent_ids),
        "checks_silent_ids": sorted(silent_ids),
        "coverage": round(checks_available / checks_total, 3) if checks_total else None,
    }
    # Below this, the number stops meaning anything: a source serving a fifth of
    # the registry would score a site on almost no evidence. Suppressing is the
    # same discipline as everywhere else — better a stated absence than a
    # confident wrong number. Note the score is deliberately NOT rescaled by
    # coverage: estimating what the checks that never ran would have found is
    # invention, so comparability is refused instead of faked.
    coverage_ratio = summary["check_coverage"]["coverage"] or 0.0
    if coverage_ratio < MIN_COVERAGE_TO_SCORE and summary["health_score"] is not None:
        summary["health_score"] = None
        summary["health_score_reason"] = (
            f"only {checks_available} of {checks_total} checks could run "
            f"({coverage_ratio:.0%} coverage); too little evidence to score"
        )

    if checks_skipped or checks_disabled:
        summary["health_score_basis"] = (
            f"{checks_available} of {checks_total} checks could run; the score is not "
            "comparable to a run with full evidence"
        )

    # A crawl far below the declared sitemap still scores, but says so: the
    # score describes what was crawled, not the site.
    run["crawl_partial"] = crawl_partial
    if sitemap_partial:
        summary["health_score_scope"] = (
            f"{urls_crawled} of {urls_in_sitemap} sitemap URLs crawled — "
            "the score describes the crawled subset, not the whole site"
        )
    if size_stats:
        summary["size_stats_bytes"] = {k: int(v) for k, v in size_stats.items() if k != "iqr"}
    if sitemap_summary:
        summary["sitemap"] = sitemap_summary

    # cap pages in JSON if configured
    max_pages = ctx.config.get("output", {}).get("max_pages_in_json", 100000)
    pages = ctx.pages[:max_pages]

    # Same partition computed above the fold, reused here rather than
    # recomputed, so the detail records agree with the summary counts by
    # construction (issue #177).
    skipped = [s for s in ctx.skipped if s.id in declared_ids]
    disabled = [SkippedCheck(id=cid, reason="disabled in config") for cid in sorted(disabled_ids)]

    return AuditResult(
        run=run,
        summary=summary,
        issues=issues,
        pages=pages,
        groups=ctx.groups,
        skipped=skipped,
        disabled=disabled,
    )
