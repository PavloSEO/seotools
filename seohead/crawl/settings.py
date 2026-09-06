"""Crawler configuration: defaults, precedence, validation, and the run manifest.

Three properties matter more than the field list.

**A setting that nothing reads is a lie.** Every key here is wired to behaviour.
Unknown keys are rejected with their path rather than ignored, so a typo in a
scope pattern cannot silently widen a crawl and a setting cannot be added to the
file before it is added to the code.

**Store and crawl are different questions.** For each link type, "keep it in the
report" and "request it for a status code" are independent. Collapsing them into
one flag is why a crawler either misses broken images or triples its requests.

**Two thirds of these settings change what the audit finds.** Those are recorded
in the run manifest, because otherwise two reports on the same site are not
comparable and nobody can tell why they differ. The rest change only how long the
run takes. ``RESULTS_AFFECTING`` is that classification, and a test fails when a
new setting is added without being placed in one group or the other.
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

# The largest crawl this tool will attempt, and the reason for a number rather than
# "no limit". Both result.pages and result.links are held for the whole run --
# links.jsonl is a resume aid, not a memory bound, since spider.py appends every edge
# to the in-memory list as well.
#
# The 383-byte LinkEdge and 2 400-byte PageRecord figures came from tracemalloc
# fixtures over 40 000 edges and 8 000 records with distinct URL/text strings;
# summing sys.getsizeof over a shared object graph roughly doubles them. The paired
# iframe-head/combined-record fixture measures a 56-byte empty-hreflang-list
# increment; the eight fields added for the #385/#386 coverage checks, and the two
# added for the AJAX-crawling-scheme checks (#386), are empty strings and zeros by
# default and measure no further allocation either -- the 56-field and 58-field
# records were measured side by side with one instrument and came back identical.
# Field lengths affect absolute memory,
# so 2 456 bytes is an approximate combined PageRecord estimate; the rounded totals are
#
#   10 000 URLs x 150 links/page -> 0.56 GiB
#   50 000 URLs x  60 links/page -> 1.18 GiB
#   50 000 URLs x 150 links/page -> 2.79 GiB
#
# so the honest ceiling depends on how densely the site links, which no constant can
# know. 50 000 is where a full crawl stops being the right instrument anyway; past it
# the answer is to narrow the scope (scope.include_patterns, scope.exclude_patterns)
# rather than to raise this. A request above the ceiling is refused, not quietly
# reduced: a run that fetched 50 000 of the 200 000 URLs asked for is a partial crawl,
# and the caller has to know that before the audit is believed (#356).
MAX_URLS_CEILING = 50_000


def checked_url_budget(max_urls: int) -> int:
    """How many URLs a crawl may fetch -- or a refusal, never a quiet reduction.

    ``min(max_urls, MAX_URLS_CEILING)`` was the old answer and it was wrong in the
    one case that matters: a caller who asked for 200 000 URLs was given 50 000 and
    a result that did not say so, so a quarter of a site was reported as the whole
    of it. Both crawl entry points call this, which is also what stops the ceiling
    from being defined twice and drifting apart again (#356).
    """
    budget = max(1, int(max_urls))
    if budget > MAX_URLS_CEILING:
        raise ValueError(
            f"max_urls is {budget:,}, above this crawler's ceiling of {MAX_URLS_CEILING:,}; "
            "narrow the scope rather than raising the budget"
        )
    return budget


DEFAULTS: dict[str, Any] = {
    "scope": {
        # Which discovered URLs count as internal. "host" is the conservative
        # reading; "registrable_domain" also accepts subdomains.
        "internal": "host",  # host | registrable_domain
        # Regexes searched against the whole URL of every *discovered* link.
        # The start URL is always fetched: a crawl that filters out its own seed
        # would report an empty site rather than a configuration mistake.
        "include_patterns": [],
        "exclude_patterns": [],
        # Never fetched regardless of what links to them.
        "exclude_hosts": [],
        # Ordered, first-match-wins named segments -- a multilingual or multi-regional
        # site is several sites sharing one crawl, and until a URL is assigned to one
        # of them nothing downstream can group by it (#358). Each entry is
        # {"name": ..., plus at least one of "prefix" (path prefix), "host" (exact
        # hostname -- a subdomain or a wholly separate domain) or "pattern" (regex
        # over the whole URL)}. A URL matching none of them is the built-in
        # "default" segment, so every URL belongs to exactly one segment, always.
        "segments": [],
        # Fetch only these segment names (plus "default" when named) -- the thing
        # that makes crawling one region of a large site cheap without an ad-hoc
        # scope.include_patterns regex. Empty means every segment is in scope.
        "segments_only": [],
    },
    "sitemaps": {
        # Seed the crawl from the sitemap declared in robots.txt (the
        # ``Sitemap:`` directive) when no explicit sitemap URL is given.
        # Every declared URL is fetched and its own links are followed,
        # rather than the sitemap being treated as the final answer.
        "auto_discover": False,
    },
    "discovery": {
        # Each link type is a pair: store it in the report, and/or request it. Only the pairs
        # that actually change an outcome are here. discovery.canonicals.* and
        # discovery.external.crawl were removed in the #91 pass: chasing canonicals as a
        # discovery source and crawling a second host are capabilities the spider does not
        # have, and a setting that names behaviour nothing implements is worse than no setting
        # — it reads as a supported option in --config-help and in the run manifest.
        "hyperlinks": {"store": True, "crawl": True},
        "redirects": {"crawl": True},
        # A redirect has no LinkEdge of its own to withhold, so there is no redirects.store to
        # honour: the redirect target is recorded on the page record either way.
        "external": {"store": True},
        "follow_nofollow": False,
        # List mode only (no ``url``, only ``urls``): a redirect is recorded as
        # given -- the hop is never followed as a new page -- but a migration
        # audit needs to know where the chain actually ends, not just that a
        # hop exists. Off by default because it is extra requests per redirect
        # a plain status check does not need.
        "resolve_redirect_destination": False,
    },
    "limits": {
        "max_urls": 200,
        "max_depth": 5,
        "max_query_variants_per_path": 5,
        "max_response_bytes": 5 * 1024 * 1024,
        "max_url_length": 2000,
        "max_crawl_seconds": 0,  # 0 = no wall-clock limit
    },
    "http": {
        "timeout_seconds": 15.0,
        "user_agent": "",  # empty = the toolkit's identifiable default
        "headers": {},
        "retry_on_timeout": 0,
        # Each entry is {"host": "...", "headers": {"Authorization": "env:VAR"}}.
        # Bound to one host and resolved from the environment — never a bare
        # value in the file — so a credential cannot leak into a config export
        # and cannot be sent to a host it was never meant for.
        "credential_headers": [],
        # Authenticated crawling clicks every link while logged in, including
        # ones that log out, publish, or delete. Requiring this flag once
        # credential_headers is non-empty makes that risk an explicit choice.
        "credentials_acknowledged": False,
    },
    "robots": {
        # respect: obey. report_only: fetch, report what would be blocked, crawl
        # anyway — the honest audit setting. ignore: do not fetch it at all.
        "policy": "respect",  # respect | report_only | ignore
        "user_agent_token": "SEOHEAD-Tools",
        "unavailable_means_stop": True,
    },
    "speed": {
        "min_delay_seconds": 0.5,
        "max_delay_seconds": 60.0,
        "adaptive": True,
        "stop_after_consecutive_timeouts": 5,
        # A per-origin ceiling on requests in flight at once, not a promise:
        # the crawler starts well below it and the adaptive throttle only
        # widens toward it on sustained success. 1 is the sequential crawler.
        "concurrency": 1,
    },
    "output": {
        "dir": "",
        "write_pages_jsonl": True,
        # A structured, per-URL decision log beside pages.jsonl (issue #134):
        # measured at one small JSON line per exclusion on the chain fixture,
        # so on by default rather than a diagnostic nobody remembers to enable.
        "write_decisions_jsonl": True,
        # The prioritized backlog beside audit.json. build_tasks has always
        # taken an audit document and a native crawl has always produced one,
        # but nothing joined them: the backlog was reachable only through
        # `sf run --tasks`, so a crawl done without Screaming Frog produced
        # findings and no list of what to do about them. It is pure computation
        # over the audit already in memory -- no network, no second pass --
        # which is why it is on rather than something to remember.
        "write_tasks": True,
    },
    "link_position": {
        # Off by default: classifying every link's DOM ancestry (nav, header,
        # sidebar, footer, content) has a real per-link cost, and most crawls
        # never read the result. See tools/link_position.py.
        "classify": False,
        # Overrides the built-in nav/header/sidebar/footer selectors. Each
        # entry is {"position": ..., "selector": ...}; empty keeps the
        # built-ins. Plenty of production menus are not a <nav> element.
        "rules": [],
    },
    "link_attributes": {
        # Off by default, same reasoning as link_position.classify just above: the full
        # rel-token set, target attribute, and raw (pre-resolution) href add ~50% to
        # per-edge memory on a large crawl (measured on a synthetic 3387-page/
        # 150-link-per-page corpus, ~508k edges: roughly +95 bytes/edge, ~46 MiB total --
        # see LinkEdge's own docstring in crawl/spider.py for the breakdown, issue #125).
        # Only unsafe-cross-origin-link and protocol-relative-link detection need this;
        # the per-target follow/nofollow aggregation and the localhost-outlink check use
        # only fields already stored regardless of this setting.
        "capture": False,
    },
    "cache": {
        # off (default): no cache, reads or writes — a crawl has exactly the side effects it
        # had before this setting existed, per this project's own rule that a side effect (here,
        # writing response bodies to a fixed location outside any explicit output directory)
        # must be explicit, never hidden behind a default. live: real HTTP freshness semantics
        # (max-age/Expires, ETag/Last-Modified revalidation). replay: serve whatever is on disk
        # for a URL that already has an entry, at any age, without ever revalidating it — a URL
        # with no entry is still fetched live. See seohead.crawl.cache for the full policy.
        "mode": "off",
        # Force every lookup to miss (a live measurement happens) while still writing the
        # result back to the cache. A deliberate hard refresh, distinct from mode="off" -- and
        # rejected by validate() when mode="replay", since replay's own promise is the opposite
        # (never touch the network for an entry already on disk).
        "invalidate": False,
    },
    "resources": {
        "fetch": False,
        "max_requests": 20_000,
        "max_response_bytes": 5 * 1024 * 1024,
    },
    "storage": {
        "body_mode": "captured_entity_bytes",
        "max_body_bytes": 5 * 1024 * 1024,
        "max_body_store_bytes": 10 * 1024 * 1024 * 1024,
        "min_free_bytes": 1024 * 1024 * 1024,
        "history_warning_bytes": 20 * 1024 * 1024 * 1024,
    },
    "rendering": {
        # raw: static HTML only, the crawler's original behaviour. legacy_
        # fragment: honour a page's own opt-in to the deprecated "#!" / "
        # ?_escaped_fragment_=" AJAX-crawling scheme when it declares one, no
        # browser required. js: execute JavaScript in a headless browser and
        # extract from the rendered DOM -- always in addition to, never
        # instead of, the links already found in the raw HTML, because a
        # link hydration removes is a real finding, not a link that never
        # existed. See seohead.crawl.render_escalation for how "js" and
        # "legacy_fragment" are applied selectively rather than to every URL.
        "mode": "raw",  # raw | legacy_fragment | js
        "escalation": {
            # How many URLs per detected template pattern are probed
            # raw-versus-fuller before the whole pattern is escalated.
            # Sampling patterns, not every URL, is what keeps rendering an
            # order of magnitude cheaper than rendering the whole crawl.
            "sample_per_pattern": 2,
            # A budget for the re-fetch step, separate from the static
            # crawl's own limits.max_urls: escalating every page of a large
            # pattern would erase the saving sampling was meant to buy.
            "max_render_urls": 30,
            "max_render_seconds": 0,  # 0 = no wall-clock limit
        },
        "browser": {
            # How long JavaScript may keep running after the page and its
            # subresources have loaded. Too short loses content on a slow
            # application; too long multiplies crawl duration -- there is no
            # universally correct value, which is why it is recorded.
            "script_timeout_seconds": 10.0,
            # A responsive page renders a different DOM at different widths,
            # so word count and link count change with this setting.
            # "desktop" and "mobile" are the only presets (see
            # seohead.tools.render.VIEWPORT_PRESETS) so two runs are only
            # ever comparable by name, never by an arbitrary pixel value.
            "viewport": "desktop",  # desktop | mobile
            # Grow the viewport to the rendered page's own height so lazily
            # loaded listings are captured, capped by
            # resize_to_content_max_height_px so a page that grows without
            # bound is truncated deterministically, not crawled forever.
            "resize_to_content": False,
            "resize_to_content_max_height_px": 15000,
            # Both match how a search engine assembles a page from what a
            # user's browser actually renders, and both change the extracted
            # content: a component's markup that lives only inside a shadow
            # root, or only inside a same-origin iframe, is otherwise absent
            # from page.content() entirely.
            "flatten_shadow_dom": False,
            "flatten_iframes": False,
            "device_pixel_ratio": 1.0,
            "mobile_emulation": False,
            "touch_emulation": False,
            # What counts as "loaded" before script_timeout_seconds starts
            # counting down. "networkidle" may never occur on a commercial
            # site (analytics, chat, ads keep connections open), turning a
            # useful render into a timeout -- see render.render_check.
            "wait_until": "load",  # load | domcontentloaded | networkidle
            # Off by default, and refused without an explicit directory
            # (see validate() below): attaching a real browser profile
            # crawls the site as whoever's cookies that profile carries.
            "persistent_profile": False,
            "persistent_profile_dir": "",
        },
        "artifacts": {
            # Both off by default: heavy on disk, and most audits need
            # neither. Screenshots and console errors are per-URL artefacts,
            # not audit findings, so they never change what is found.
            "screenshots": False,
            "console_errors": False,
        },
    },
}

# Settings that can change what the audit finds. These go into the manifest.
# Everything else changes only duration or resource use.
RESULTS_AFFECTING: frozenset[str] = frozenset(
    {
        "scope.internal",
        "scope.include_patterns",
        "scope.exclude_patterns",
        "scope.exclude_hosts",
        # A host-matching segment widens which hosts count as internal, and
        # segments_only narrows the frontier to named segments -- both change what
        # is fetched, and segments alone also changes the audit's per-segment
        # breakdown even when nothing is excluded.
        "scope.segments",
        "scope.segments_only",
        # Seeding from the sitemap changes which URLs are fetched at all.
        "sitemaps.auto_discover",
        "discovery.hyperlinks.store",
        "discovery.hyperlinks.crawl",
        "discovery.redirects.crawl",
        "discovery.external.store",
        "discovery.follow_nofollow",
        "discovery.resolve_redirect_destination",
        # Every limit truncates the corpus, and a truncated crawl produces false
        # "not linked from anywhere" conclusions.
        "limits.max_urls",
        "limits.max_depth",
        "limits.max_query_variants_per_path",
        "limits.max_response_bytes",
        "limits.max_url_length",
        "limits.max_crawl_seconds",
        # A short timeout turns slow pages into "no response"; the user agent and
        # headers change what a UA- or locale-adaptive site serves.
        "http.timeout_seconds",
        "http.user_agent",
        "http.headers",
        "http.retry_on_timeout",
        # Which host gets sent extra access changes what the crawl can reach.
        "http.credential_headers",
        "http.credentials_acknowledged",
        "robots.policy",
        "robots.user_agent_token",
        "robots.unavailable_means_stop",
        # Politeness is normally cost-only, but a delay low enough to degrade a
        # server turns healthy pages into timeouts, and the audit then measures
        # the crawler rather than the site.
        "speed.min_delay_seconds",
        "speed.adaptive",
        "speed.stop_after_consecutive_timeouts",
        # Whether links are classified changes whether the inlink-composition
        # finding can be computed at all, and which rules classify them
        # changes where any given link lands.
        "link_position.classify",
        "link_position.rules",
        # Whether unsafe-cross-origin-link and protocol-relative-link detection can find
        # anything at all -- see LinkEdge's own docstring in crawl/spider.py.
        "link_attributes.capture",
        # A replay run can answer entirely from a stale cache; a forced-invalidate run trusted
        # nothing on disk. Both change whether the findings describe the site now or earlier.
        "cache.mode",
        "cache.invalidate",
        "storage.body_mode",
        "resources.fetch",
        "resources.max_requests",
        "resources.max_response_bytes",
        "storage.max_body_bytes",
        "storage.max_body_store_bytes",
        "storage.min_free_bytes",
        "storage.history_warning_bytes",
        # Every rendering setting below changes what the crawl finds on the
        # patterns it escalates -- see seohead.tools.render's module
        # docstring on why raw and rendered numbers are not comparable
        # unless the settings that produced each are recorded.
        "rendering.mode",
        "rendering.escalation.sample_per_pattern",
        "rendering.escalation.max_render_urls",
        "rendering.escalation.max_render_seconds",
        "rendering.browser.script_timeout_seconds",
        "rendering.browser.viewport",
        "rendering.browser.resize_to_content",
        "rendering.browser.resize_to_content_max_height_px",
        "rendering.browser.flatten_shadow_dom",
        "rendering.browser.flatten_iframes",
        "rendering.browser.device_pixel_ratio",
        "rendering.browser.mobile_emulation",
        "rendering.browser.touch_emulation",
        "rendering.browser.wait_until",
        # A different profile crawls as a different, possibly logged-in,
        # visitor; the directory itself is not included here (nor in the
        # manifest below) for the same reason a credential's value is not:
        # a shareable manifest should not carry a local filesystem path.
        "rendering.browser.persistent_profile",
    }
)

# One-line descriptions, keyed by the same dotted paths as DEFAULTS/RESULTS_AFFECTING. This is the
# single source for every surface that lists settings for a human or an agent — the CLI's
# --config-help and, eventually, an MCP "describe settings" tool (#23) — so the three cannot drift
# into different descriptions of the same setting. A test fails if a DEFAULTS path has no entry here.
DESCRIPTIONS: dict[str, str] = {
    "resources.fetch": "SQLite only: opt in to fetching directly declared same-origin scripts and stylesheets; never follows CSS imports or JavaScript modules.",
    "resources.max_requests": "SQLite only: maximum resource HTTP attempts, including redirects and retries; independent of the page URL limit.",
    "resources.max_response_bytes": "SQLite only: maximum content-decoded bytes per resource response; total crawl time and body-store limits still apply.",
    "storage.body_mode": "SQLite only: captured_entity_bytes retains fetched HTML/DOM; off retains metadata only.",
    "storage.max_body_bytes": "SQLite only: maximum decoded bytes retained for one complete body.",
    "storage.max_body_store_bytes": "SQLite only: total unique encoded body bytes retained per scan.",
    "storage.min_free_bytes": "SQLite only: filesystem reserve; low space interrupts collection with a checkpoint.",
    "storage.history_warning_bytes": "SQLite only: history-size warning threshold; never enables automatic deletion.",
    "scope.internal": (
        "Which discovered URLs count as internal: 'host' (conservative) or "
        "'registrable_domain' (also accepts subdomains -- on a shared hosting suffix such as "
        "github.io or vercel.app, only the audited tenant's own subdomain, never another "
        "tenant's, per seohead.recon.net.registrable_domain)."
    ),
    "scope.include_patterns": "Regexes; a discovered link must match at least one to be followed.",
    "scope.exclude_patterns": "Regexes; a discovered link matching any of these is not followed.",
    "scope.exclude_hosts": "Hosts never fetched regardless of what links to them.",
    "scope.segments": (
        "Ordered, first-match-wins named segments for a multilingual or multi-regional "
        "site: [{'name': ..., 'prefix': '/en/', 'host': 'en.example.com', "
        "'pattern': '...'}, ...] -- at least one of prefix/host/pattern per entry. A "
        "URL matching none of them is the built-in 'default' segment, so every URL "
        "belongs to exactly one segment, always."
    ),
    "scope.segments_only": (
        "Fetch only these segment names (plus 'default' when named); empty means "
        "every segment is in scope. Subsumes a subfolder-only or single-subdomain "
        "crawl without an ad-hoc scope.include_patterns regex."
    ),
    "sitemaps.auto_discover": (
        "Seed the crawl from the sitemap declared in robots.txt when no explicit "
        "sitemap URL is given."
    ),
    "discovery.hyperlinks.store": "Keep discovered hyperlinks in the report.",
    "discovery.hyperlinks.crawl": "Request discovered hyperlinks (fetch them).",
    "discovery.redirects.crawl": "Request discovered redirect targets (fetch them).",
    "discovery.external.store": "Keep discovered external links in the report.",
    "discovery.follow_nofollow": "Follow links marked rel=nofollow instead of skipping them.",
    "discovery.resolve_redirect_destination": (
        "List mode only: follow a fetched redirect past its first hop to where it actually "
        "lands, recording every hop. Depth stays 0; this is a per-URL chain walk, not link "
        "discovery."
    ),
    "limits.max_urls": (
        "Maximum number of URLs the crawl will fetch. Values above "
        f"{MAX_URLS_CEILING:,} are refused rather than clamped; narrow the scope instead."
    ),
    "limits.max_depth": "Maximum link depth from the start URL.",
    "limits.max_query_variants_per_path": "Maximum distinct query strings kept per URL path.",
    "limits.max_response_bytes": "Response bodies larger than this are truncated before parsing.",
    "limits.max_url_length": "URLs longer than this are not fetched.",
    "limits.max_crawl_seconds": "Wall-clock budget for the whole crawl; 0 means no limit.",
    "http.timeout_seconds": "Per-request timeout in seconds.",
    "http.user_agent": "Request User-Agent string; empty uses the toolkit's identifiable default.",
    "http.headers": (
        "Extra request headers to send with every fetch. With --set, pass a JSON object."
    ),
    "http.retry_on_timeout": "Number of retries after a request times out.",
    "http.credential_headers": (
        "Host-bound extra headers for authenticated crawling: "
        "[{'host': ..., 'headers': {name: 'env:VAR_NAME'}}]."
    ),
    "http.credentials_acknowledged": "Must be true for http.credential_headers to take effect.",
    "robots.policy": (
        "'respect' (obey), 'report_only' (fetch, report what would be blocked, crawl "
        "anyway), or 'ignore' (do not fetch robots.txt)."
    ),
    "robots.user_agent_token": "The User-Agent token matched against robots.txt rules.",
    "robots.unavailable_means_stop": "Stop the crawl if robots.txt cannot be fetched at all.",
    "speed.min_delay_seconds": "Minimum delay between requests; the floor beneath adaptive back-off.",
    "speed.max_delay_seconds": "Maximum delay adaptive back-off may reach.",
    "speed.adaptive": "Increase the delay automatically when the target slows down or times out.",
    "speed.stop_after_consecutive_timeouts": "Stop the crawl after this many timeouts in a row.",
    "speed.concurrency": (
        "Per-origin ceiling on requests in flight at once. The adaptive throttle starts "
        "well below it and only grows toward it on sustained success; 1 is sequential."
    ),
    "output.dir": "Directory to write pages.jsonl and audit.json into; empty writes nothing to disk.",
    "output.write_pages_jsonl": "Write one JSON line per fetched page to pages.jsonl.",
    "output.write_decisions_jsonl": (
        "Write one JSON line per exclusion decision to decisions.jsonl, naming the URL and the "
        "rule that rejected it — see seohead.tools.logscan for what reads it."
    ),
    "output.write_tasks": (
        "Write the prioritized backlog beside audit.json as tasks.json and tasks.md — the same "
        "pipeline `sf run --tasks` uses, run over this crawl's own audit."
    ),
    "link_position.classify": (
        "Classify each link's DOM ancestry (nav/header/sidebar/footer/content) as it is "
        "parsed, at no extra requests; off by default because storing a position per link "
        "costs memory on a large crawl."
    ),
    "link_position.rules": (
        "Ordered [{'position', 'selector'}] overrides for the built-in nav/header/sidebar/"
        "footer selectors; empty keeps the built-ins. Only read when classify is true."
    ),
    "link_attributes.capture": (
        "Store each link's full rel tokens, target attribute, and raw (pre-resolution) href "
        "-- needed for unsafe-cross-origin-link and protocol-relative-link detection, at a "
        "real memory cost on a large crawl."
    ),
    "cache.mode": (
        "'off' (default: no cache), 'live' (real HTTP freshness: max-age/Expires, "
        "ETag/Last-Modified revalidation), or 'replay' (serve any cached entry regardless of "
        "age, without revalidating; a URL with no entry yet is still fetched live)."
    ),
    "cache.invalidate": (
        "Force every cache lookup to miss (a live measurement happens) while still writing the "
        "result back to the cache. Does not disable the cache; 'cache.mode=off' does that. "
        "Rejected together with cache.mode='replay', which promises the opposite guarantee."
    ),
    "rendering.mode": (
        "'raw' (static HTML only), 'legacy_fragment' (honour a page's own "
        "'_escaped_fragment_' opt-in), or 'js' (execute JavaScript in a headless "
        "browser, selectively -- see rendering.escalation)."
    ),
    "rendering.escalation.sample_per_pattern": (
        "URLs probed raw-versus-fuller per detected template pattern before deciding "
        "whether the whole pattern needs escalation."
    ),
    "rendering.escalation.max_render_urls": (
        "Maximum number of pages re-fetched under the fuller representation, across all "
        "escalated patterns combined."
    ),
    "rendering.escalation.max_render_seconds": (
        "Wall-clock budget for the escalation step; 0 means no limit."
    ),
    "rendering.browser.script_timeout_seconds": (
        "How long JavaScript may keep running after the page and its subresources have "
        "loaded, before the DOM is captured."
    ),
    "rendering.browser.viewport": "Viewport preset used for rendering: 'desktop' or 'mobile'.",
    "rendering.browser.resize_to_content": (
        "Grow the viewport to the rendered page's own height before capture, capped by "
        "resize_to_content_max_height_px."
    ),
    "rendering.browser.resize_to_content_max_height_px": (
        "Deterministic cap on resize_to_content, so an unbounded page is truncated rather "
        "than crawled forever."
    ),
    "rendering.browser.flatten_shadow_dom": (
        "Merge open shadow roots into their host elements before capturing the DOM."
    ),
    "rendering.browser.flatten_iframes": (
        "Replace same-origin iframes with their own document's body content before capture."
    ),
    "rendering.browser.device_pixel_ratio": "Device scale factor used for rendering.",
    "rendering.browser.mobile_emulation": "Emulate a mobile device (touch UA, is_mobile).",
    "rendering.browser.touch_emulation": "Emulate touch input.",
    "rendering.browser.wait_until": (
        "Page-load strategy before script_timeout_seconds starts counting down: 'load', "
        "'domcontentloaded', or 'networkidle'."
    ),
    "rendering.browser.persistent_profile": (
        "Attach a persistent browser profile instead of an anonymous one. Off by default: "
        "a real profile crawls the site as whoever's cookies it carries."
    ),
    "rendering.browser.persistent_profile_dir": (
        "Directory for the persistent profile; required when persistent_profile is true, "
        "and never a computed default."
    ),
    "rendering.artifacts.screenshots": "Save a full-page screenshot per rendered URL.",
    "rendering.artifacts.console_errors": "Capture browser console error messages per rendered URL.",
}

# Environment overrides, applied between the file and explicit arguments.
ENV_OVERRIDES: dict[str, str] = {
    "SEOHEAD_CRAWL_MAX_URLS": "limits.max_urls",
    "SEOHEAD_CRAWL_MAX_DEPTH": "limits.max_depth",
    "SEOHEAD_CRAWL_MIN_DELAY": "speed.min_delay_seconds",
    "SEOHEAD_CRAWL_ROBOTS": "robots.policy",
    "SEOHEAD_CRAWL_USER_AGENT": "http.user_agent",
}

ROBOTS_POLICIES = ("respect", "report_only", "ignore")
INTERNAL_SCOPES = ("host", "registrable_domain")
# The segment every URL falls into when it matches none of scope.segments. Reserved:
# an operator cannot declare a segment under this name, since it would collide with
# the one every unmatched URL already gets (#358).
DEFAULT_SEGMENT = "default"
_SEGMENT_KEYS = frozenset({"name", "prefix", "host", "pattern"})
CACHE_MODES = ("live", "off", "replay")
RENDER_MODES = ("raw", "legacy_fragment", "js")
RENDER_VIEWPORTS = ("desktop", "mobile")
RENDER_WAIT_UNTIL = ("load", "domcontentloaded", "networkidle")

# A reference to an environment variable, never an inline secret. This is the
# only value shape a credential header may carry in a config file.
_ENV_REF_RE = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]*)$")

# Applied automatically to the crawl scope once credential_headers is set. A
# crawler clicks every link, and while logged in that includes links that log
# out, publish, or delete — this is a default, not a guarantee, and a caller
# who has audited the site can still widen scope.exclude_patterns.
DESTRUCTIVE_PATH_PATTERNS: tuple[str, ...] = (
    r"(?i)/(log[-_]?out|sign[-_]?out)(?:/|$|\?)",
    r"(?i)/(delete|remove|destroy|unsubscribe)(?:/|$|\?)",
)


class ConfigError(ValueError):
    """A configuration that cannot be trusted to mean what it says."""


def _flatten(mapping: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted paths to leaf values. Free-form maps are leaves, not branches."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and path not in ("http.headers",):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _set_path(mapping: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = mapping
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _coerce(path: str, raw: str) -> Any:
    """Environment and command-line values arrive as strings; give them the default's type."""
    current = _flatten(DEFAULTS).get(path)
    if isinstance(current, bool):
        value = raw.strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
        raise ValueError("expected true or false")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        # A list setting is a set of patterns or hosts; an empty string means the
        # empty list rather than a list containing one empty pattern, which would
        # match everything and silently widen a crawl.
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(current, dict):
        value = json.loads(raw)
        if not isinstance(value, dict) or any(
            not isinstance(name, str) or not isinstance(header, str)
            for name, header in value.items()
        ):
            raise ValueError("expected a JSON object with string keys and values")
        return value
    return raw


def parse_setting_assignment(text: str) -> tuple[str, Any]:
    """Turn one ``path=value`` argument into a validated override.

    The path is checked against DEFAULTS here rather than at validate() time, so a
    typo is reported as a typo -- naming the setting the operator meant to reach --
    instead of surfacing later as an unknown-key error about a path nobody typed.
    """
    path, sep, raw = text.partition("=")
    path = path.strip()
    if not sep or not path:
        raise ConfigError(f"--set expects PATH=VALUE, got {text!r}")
    known = _flatten(DEFAULTS)
    if path not in known:
        near = [k for k in sorted(known) if k.split(".")[-1] == path.split(".")[-1]]
        hint = f"; did you mean {near[0]}?" if near else "; see --config-help"
        raise ConfigError(f"unknown setting {path!r}{hint}")
    try:
        return path, _coerce(path, raw)
    except ValueError as exc:
        raise ConfigError(f"{path}={raw!r} is not valid: {exc}") from exc


def delay_for_request_rate(rate: float) -> float:
    """The ``speed.min_delay_seconds`` that caps a crawl at ``rate`` requests/second.

    The inverse of ``effective_request_rate``. Politeness is the number an operator
    actually has in mind -- a site owner says "no more than seven a second", not
    "at least 0.1428 seconds apart" -- and making them invert it themselves is how
    a decimal point in the wrong place becomes somebody's site under load.
    """
    if rate <= 0:
        raise ConfigError("--max-urls-per-second must be greater than 0")
    return 1.0 / rate


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def validate(config: dict[str, Any]) -> None:
    """Reject anything that cannot be honoured, naming the offending path."""
    known = set(_flatten(DEFAULTS))
    for path in _flatten(config):
        if path not in known:
            raise ConfigError(
                f"unknown setting {path!r}. A setting the crawler does not read would "
                "promise behaviour that does not exist"
            )

    robots = config["robots"]["policy"]
    if robots not in ROBOTS_POLICIES:
        raise ConfigError(f"robots.policy must be one of {ROBOTS_POLICIES}, got {robots!r}")
    scope = config["scope"]["internal"]
    if scope not in INTERNAL_SCOPES:
        raise ConfigError(f"scope.internal must be one of {INTERNAL_SCOPES}, got {scope!r}")
    cache_mode = config["cache"]["mode"]
    if cache_mode not in CACHE_MODES:
        raise ConfigError(f"cache.mode must be one of {CACHE_MODES}, got {cache_mode!r}")
    # "replay" promises it never touches the network for a URL already on disk;
    # "invalidate" promises the opposite -- a live measurement on every lookup.
    # Both are explicit, deliberate settings, so silently letting one win (issue
    # #137) is worse than refusing the combination outright.
    if cache_mode == "replay" and config["cache"]["invalidate"]:
        raise ConfigError(
            "cache.invalidate cannot be combined with cache.mode='replay': replay promises "
            "to serve on-disk entries without ever touching the network, which invalidate "
            "exists to override. Pick one: cache.mode='live' with cache.invalidate=true for "
            "a forced hard refresh, or drop invalidate to keep replaying from disk."
        )
    if "resources" in config:
        resources = config["resources"]
        if type(resources["fetch"]) is not bool:
            raise ConfigError("resources.fetch must be boolean")
        for name in ("max_requests", "max_response_bytes"):
            value = resources[name]
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ConfigError(f"resources.{name} must be a nonnegative SQLite-sized integer")
    if "storage" in config:
        storage = config["storage"]
        if storage["body_mode"] not in {"off", "captured_entity_bytes"}:
            raise ConfigError("storage.body_mode must be off or captured_entity_bytes")
        for name in (
            "max_body_bytes",
            "max_body_store_bytes",
            "min_free_bytes",
            "history_warning_bytes",
        ):
            value = storage[name]
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ConfigError(f"storage.{name} must be a nonnegative SQLite-sized byte count")
        if storage["body_mode"] != "off" and storage["max_body_bytes"] == 0:
            raise ConfigError(
                "storage.max_body_bytes must be positive when body capture is enabled"
            )
    # A pattern that does not compile would otherwise fail mid-crawl, after the
    # site has already been asked for a few hundred pages.
    for key in ("include_patterns", "exclude_patterns"):
        for pattern in config["scope"][key] or ():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"scope.{key}: {pattern!r} is not a valid regex: {exc}") from exc

    limits = config["limits"]
    if limits["max_urls"] < 1:
        raise ConfigError("limits.max_urls must be at least 1")
    if limits["max_urls"] > MAX_URLS_CEILING:
        raise ConfigError(
            f"limits.max_urls is {limits['max_urls']:,}, above this crawler's ceiling of "
            f"{MAX_URLS_CEILING:,}. A larger number would have been silently reduced to the "
            f"ceiling and the crawl reported as complete; crawl a narrower scope "
            f"(scope.include_patterns / scope.exclude_patterns) instead."
        )
    if limits["max_depth"] < 0:
        raise ConfigError("limits.max_depth cannot be negative")
    if limits["max_query_variants_per_path"] < 0:
        # 0 is the crawler's own "unlimited" (see spider.py's truthy check on this
        # value); a negative number is not a smaller budget, it makes every
        # comparison against it (`len(variants) >= cap`) true immediately, which
        # rejects the first query URL on every path and every one after it (#195).
        raise ConfigError("limits.max_query_variants_per_path cannot be negative")
    if config["speed"]["min_delay_seconds"] < 0:
        raise ConfigError("speed.min_delay_seconds cannot be negative")
    if config["speed"]["concurrency"] < 1:
        raise ConfigError("speed.concurrency must be at least 1")

    # A crawl with no budget at all runs forever on an infinite URL space.
    if not limits["max_urls"] and not limits["max_depth"] and not limits["max_crawl_seconds"]:
        raise ConfigError(
            "a crawl needs at least one budget: max_urls, max_depth or max_crawl_seconds"
        )

    for rule in config["link_position"]["rules"]:
        if not isinstance(rule, dict) or not rule.get("position") or not rule.get("selector"):
            raise ConfigError(
                f"link_position.rules entries need both 'position' and 'selector'; got {rule!r}"
            )

    _validate_segments(config["scope"])
    headers = config["http"]["headers"]
    if not isinstance(headers, dict) or any(
        not isinstance(name, str) or not isinstance(value, str) for name, value in headers.items()
    ):
        raise ConfigError("http.headers must be an object mapping header names to string values")
    _validate_credential_headers(config["http"])
    _validate_rendering(config["rendering"])


def _validate_segments(scope: dict[str, Any]) -> None:
    """Every segment is named, ordered, and matches by at least one declared rule.

    ``segments_only`` is checked against the names declared here so a typo'd
    segment name is refused before the crawl runs rather than silently scoping
    to nothing (#358).
    """
    segments = scope["segments"]
    if not isinstance(segments, list):
        raise ConfigError("scope.segments must be a list")
    names: set[str] = set()
    for entry in segments:
        if not isinstance(entry, dict):
            raise ConfigError(f"scope.segments entries must be objects, got {entry!r}")
        extra = set(entry) - _SEGMENT_KEYS
        if extra:
            raise ConfigError(f"scope.segments entry has unknown keys {sorted(extra)}: {entry!r}")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"scope.segments entry needs a non-empty 'name': {entry!r}")
        if name == DEFAULT_SEGMENT:
            raise ConfigError(
                f"scope.segments entry cannot be named {DEFAULT_SEGMENT!r}: that name is "
                "reserved for a URL matching none of the declared segments"
            )
        if name in names:
            raise ConfigError(f"scope.segments has duplicate name {name!r}")
        names.add(name)
        if not entry.get("prefix") and not entry.get("host") and not entry.get("pattern"):
            raise ConfigError(
                f"scope.segments entry {name!r} needs at least one of 'prefix', 'host' "
                "or 'pattern' to match by"
            )
        pattern = entry.get("pattern")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"scope.segments entry {name!r}: pattern {pattern!r} is not a valid "
                    f"regex: {exc}"
                ) from exc

    segments_only = scope["segments_only"]
    if not isinstance(segments_only, list):
        raise ConfigError("scope.segments_only must be a list")
    allowed = names | {DEFAULT_SEGMENT}
    unknown = [name for name in segments_only if name not in allowed]
    if unknown:
        raise ConfigError(
            f"scope.segments_only names {unknown} that scope.segments never declares "
            f"(known: {sorted(allowed)})"
        )


def _validate_rendering(rendering: dict[str, Any]) -> None:
    if rendering["mode"] not in RENDER_MODES:
        raise ConfigError(
            f"rendering.mode must be one of {RENDER_MODES}, got {rendering['mode']!r}"
        )

    escalation = rendering["escalation"]
    if escalation["sample_per_pattern"] < 1:
        raise ConfigError("rendering.escalation.sample_per_pattern must be at least 1")
    if escalation["max_render_urls"] < 0:
        raise ConfigError("rendering.escalation.max_render_urls cannot be negative")
    if escalation["max_render_seconds"] < 0:
        raise ConfigError("rendering.escalation.max_render_seconds cannot be negative")

    browser = rendering["browser"]
    if browser["viewport"] not in RENDER_VIEWPORTS:
        raise ConfigError(
            f"rendering.browser.viewport must be one of {RENDER_VIEWPORTS}, "
            f"got {browser['viewport']!r}"
        )
    if browser["wait_until"] not in RENDER_WAIT_UNTIL:
        raise ConfigError(
            f"rendering.browser.wait_until must be one of {RENDER_WAIT_UNTIL}, "
            f"got {browser['wait_until']!r}"
        )
    if browser["script_timeout_seconds"] < 0:
        raise ConfigError("rendering.browser.script_timeout_seconds cannot be negative")
    if browser["resize_to_content_max_height_px"] < 1:
        raise ConfigError("rendering.browser.resize_to_content_max_height_px must be at least 1")
    if browser["device_pixel_ratio"] <= 0:
        raise ConfigError("rendering.browser.device_pixel_ratio must be positive")
    if browser["persistent_profile"] and not browser["persistent_profile_dir"]:
        # No default directory on purpose: a default would eventually collide
        # with someone's real, cookie-carrying browser profile. Naming one is
        # the explicit choice this setting requires.
        raise ConfigError(
            "rendering.browser.persistent_profile is true but "
            "persistent_profile_dir is empty; a persistent profile must name "
            "a directory explicitly"
        )


def _validate_credential_headers(http: dict[str, Any]) -> None:
    entries = http["credential_headers"]
    if not entries:
        return
    if not http["credentials_acknowledged"]:
        raise ConfigError(
            "http.credential_headers is set but http.credentials_acknowledged is not true; "
            "a crawler clicks every link, including ones that log out, publish, or delete, "
            "so authenticated crawling needs an explicit acknowledgement"
        )
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("host"):
            raise ConfigError(
                "each http.credential_headers entry needs a host binding; an unbound "
                "credential is sent on every request and the leak is silent"
            )
        host = entry["host"]
        headers = entry.get("headers")
        if not isinstance(headers, dict) or not headers:
            raise ConfigError(f"http.credential_headers entry for {host!r} needs headers")
        for name, value in headers.items():
            match = _ENV_REF_RE.match(str(value))
            if not match:
                raise ConfigError(
                    f"http.credential_headers header {name!r} for {host!r} must reference an "
                    "environment variable as 'env:VAR_NAME', never an inline value"
                )
            if match.group(1) not in os.environ:
                raise ConfigError(
                    f"http.credential_headers for {host!r} references environment variable "
                    f"{match.group(1)!r}, which is not set"
                )


def load(path: str | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the configuration: defaults, then file, then environment, then arguments.

    The order is fixed and tested. Explicit arguments win because they are the
    most local statement of intent.
    """
    config = copy.deepcopy(DEFAULTS)

    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                from_file = json.load(handle)
        except OSError as exc:
            raise ConfigError(f"cannot read config {path!r}: {exc}") from exc
        except ValueError as exc:
            raise ConfigError(f"config {path!r} is not valid JSON: {exc}") from exc
        if not isinstance(from_file, dict):
            raise ConfigError(f"config {path!r} must contain an object")
        config = _merge(config, from_file)

    for variable, setting in ENV_OVERRIDES.items():
        raw = os.environ.get(variable)
        if raw is not None and raw != "":
            try:
                _set_path(config, setting, _coerce(setting, raw))
            except ValueError as exc:
                raise ConfigError(f"{variable}={raw!r} is not valid for {setting}: {exc}") from exc

    for setting, value in (overrides or {}).items():
        if value is None:
            continue
        # Dotted paths only. A nested mapping here replaces the whole subtree and
        # takes its siblings' defaults with it, and the failure then surfaces from
        # validate() as a bare KeyError about a key the caller never mentioned.
        if isinstance(value, dict) and not isinstance(_flatten(DEFAULTS).get(setting), dict):
            raise ConfigError(
                f"override {setting!r} is a mapping; use dotted paths such as "
                f"{setting}.{next(iter(value), 'key')} so sibling defaults survive"
            )
        _set_path(config, setting, value)

    if config["http"]["credential_headers"]:
        existing = config["scope"]["exclude_patterns"]
        config["scope"]["exclude_patterns"] = existing + [
            p for p in DESTRUCTIVE_PATH_PATTERNS if p not in existing
        ]

    validate(config)
    return config


def _redact_credential_headers(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Host and header names are what makes a run reproducible; values are not."""
    return [
        {
            "host": entry.get("host", ""),
            "headers": dict.fromkeys(entry.get("headers") or {}, "REDACTED"),
        }
        for entry in entries
    ]


def manifest(config: dict[str, Any]) -> dict[str, Any]:
    """The resolved values of every setting that can change what was found.

    Resolved values, not their sources: a report that says "the default was used"
    is not reproducible once the default moves. Credential values are the one
    exception — the manifest is meant to be shared for review, not to carry
    secrets, so only the host and header names survive here.
    """
    flat = _flatten(config)
    out = {}
    for path in sorted(RESULTS_AFFECTING):
        if path not in flat:
            continue
        value = flat[path]
        if path == "http.credential_headers":
            value = _redact_credential_headers(value)
        out[path] = value
    return out


def resolve_credential_headers(entries: list[dict[str, Any]], host: str) -> dict[str, str]:
    """Headers bound to ``host``, with their environment references resolved.

    Resolved fresh for each request rather than carried from a previous one:
    a redirect to a different host asks this again and gets nothing back,
    which is what keeps a credential from crossing a cross-host redirect.
    """
    host = (host or "").lower()
    resolved: dict[str, str] = {}
    for entry in entries or ():
        if (entry.get("host") or "").lower() != host:
            continue
        for name, ref in (entry.get("headers") or {}).items():
            match = _ENV_REF_RE.match(str(ref))
            if match:
                resolved[name] = os.environ.get(match.group(1), "")
    return resolved


def describe_settings() -> list[dict[str, Any]]:
    """Every configurable setting: its dotted path, type, default, and description.

    This is the one source that a CLI ``--config-help`` and an eventual MCP
    "describe settings" tool (#23) both read, so the two cannot drift into
    different descriptions of the same setting. Generated from ``DEFAULTS`` and
    ``DESCRIPTIONS`` rather than hand-maintained per surface.
    """
    flat = _flatten(DEFAULTS)
    out = []
    for path in sorted(flat):
        default = flat[path]
        out.append(
            {
                "path": path,
                "type": type(default).__name__,
                "default": default,
                "results_affecting": path in RESULTS_AFFECTING,
                "description": DESCRIPTIONS[path],
            }
        )
    return out


def fingerprint(config: dict[str, Any]) -> str:
    """Stable identity for "this configuration", to detect a change across runs.

    Used to invalidate a resumable crawl's checkpoint: a frontier built under
    one scope or limit set must not be resumed under another, or the result
    silently mixes rules from two different runs.
    """
    import hashlib
    import json

    payload = json.dumps(manifest(config), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def effective_request_rate(config: dict[str, Any]) -> float:
    """Worst-case requests per second this configuration permits.

    Politeness is a combination, not a single knob, so the number worth printing
    and gating on is this one rather than any individual setting.
    """
    delay = float(config["speed"]["min_delay_seconds"])
    return 1.0 / delay if delay > 0 else float("inf")
