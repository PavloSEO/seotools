"""HTTP response cache with a freshness policy that is stated, not assumed.

Fetching the same URL twice in a session is routine — a link check, then a parse, then a
render. Across sessions it is close to universal: a crawl gets re-run after a fix and most of
the corpus never changed. The hazard, stated in the issue that asked for this module, is that
the obvious default — cache everything, never revalidate, replay the last run — turns "fresh
audit" into a silent report of last week's site. This module refuses that default.

**The freshness policy, stated in full.**

- ``Cache-Control: no-store`` on the response -> never stored at all.
- ``Cache-Control: no-cache`` -> stored, but treated as already stale, so it is always
  revalidated before use (equivalent to ``max-age=0``).
- Otherwise the freshness lifetime is ``max-age`` when present, else computed from
  ``Expires`` against the response's own ``Date`` (or treated as already stale if ``Date`` is
  missing, since ``Expires`` cannot be anchored without it).
- Freshness is judged against the *corrected initial age* (RFC 9111 4.2.3), not against how
  long this cache has held the response. A response arriving through an intermediary that
  already reports ``Age`` — or whose own ``Date`` shows it was already old on arrival — starts
  its local freshness window partway spent, never restarted at zero just because SEOHEAD is the
  one receiving it now. An invalid or negative ``Age`` is ignored, never used to shrink the
  computed age (see ``corrected_initial_age``).
- No freshness information at all -> stored, but immediately stale. This is the conservative
  reading of "unstated": a page that never says how long it is good for gets treated as good
  for zero seconds, not as good forever.
- Once stale, a stored ``ETag`` or ``Last-Modified`` triggers a conditional GET
  (``If-None-Match`` / ``If-Modified-Since``) instead of a full re-fetch. A ``304`` confirms the
  stored body is still current and is recorded as a *revalidation*, never as a fresh fetch. No
  validator at all means an ordinary, unconditional re-fetch: this cache can only ever save a
  round trip, never manufacture an answer that a plain uncached crawl would not also have made.
- ``Vary`` is honoured: a response naming request headers in ``Vary`` is stored as its own
  variant, keyed on the values of exactly those headers, so a locale- or UA-adaptive URL never
  answers from the wrong representation. ``Vary: *`` means "not safely reusable" and is treated
  like ``no-store``.
- ``User-Agent`` is *always* part of the key, whether or not the origin lists it in ``Vary``.
  ``Vary`` is the origin's declaration of what it varies its own response on; it says nothing
  about which request headers this crawler's own operator can change between runs. This
  toolkit's ``http.user_agent`` setting is exactly such a header (see issue #131): two runs
  configured with different identities — a plain crawl and a "fetch as Googlebot" cloaking
  check are the motivating case — must never let one replay the body the other one stored,
  origin opinion or not. ``http.headers`` (``crawl.extra_request_headers``) has the same
  property in principle, but never reaches this far: any non-empty ``extra_headers`` already
  makes ``fetch_one`` bypass the cache outright in both directions (a credentialed request has
  always worked this way; ``http.headers`` was wired through the same gate), so it needs no key
  entry here. No other request header gets this unconditional treatment — only the one this
  crawler is documented to vary per run.
- Credentialed requests (per-host credential headers — see ``seohead.crawl.settings``) bypass
  the cache in both directions, deliberately more conservative than ``Vary`` discipline alone:
  this is a private, single-operator, local cache rather than a shared proxy, but a cache that
  can ever hand authenticated content to an unauthenticated lookup (or vice versa) is a
  correctness and safety failure worth ruling out by construction.
- ``mode="replay"`` is a separate, explicitly named mode for debugging: it serves whatever is on
  disk for a URL that already has an entry, at any age, without ever touching the network for
  it — a URL with no entry yet is still fetched live, so replay never fabricates an answer for
  something it has genuinely never seen. Every page served this way is stamped
  (``PageRecord.cache_status == "hit"``) and the run as a whole carries ``cache.mode: replay``
  in its manifest, because "the site is fine" and "the site was fine last time we looked" are
  different claims and a report must not blur them.
- ``invalidate=True`` is the explicit-invalidation escape hatch: every lookup in ``mode="live"``
  is forced to miss (a genuine live measurement happens) while stores still happen, refreshing
  the cache for next time. This is a deliberate hard refresh, distinct from ``mode="off"``,
  which disables the cache outright — reads and writes both. It is rejected in combination with
  ``mode="replay"`` (``seohead.crawl.settings.validate``), rather than silently honoured or
  silently dropped: replay's own guarantee is that it never touches the network for an entry
  already on disk, which is exactly what invalidate exists to override, so the two settings
  cannot both mean what they say at once.

**Storage and concurrency.** Entries are plain JSON, one file per (URL, Vary-selected header
values) pair, written to a temp file and then ``os.replace``'d into place — a reader never
observes a half-written entry, and two threads or processes racing on the same key simply have
the later write win, which is the correct outcome for a cache rather than a bug to guard
against with a lock. The one thing guarded by a lock is the in-memory statistics counters,
which several worker threads increment concurrently in a level-batched crawl. A cache directory
that cannot be created, or is world-writable, disables the cache rather than trusting it (the
same posture as ``seohead.crawl.state.ensure_safe_dir``, reused here directly) — unlike a
corrupted crawl checkpoint, a cache miss is always a safe, correct fallback, so a broken cache
degrades to "no cache" instead of aborting the run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

from seohead.crawl.state import ensure_safe_dir

# v2 added size_bytes. A v1 entry cannot supply it, and defaulting it to 0 would make a
# replayed page report a size it never had, so v1 entries are simply not read: the URL is
# fetched again once and stored in the new shape.
#
# #131 (User-Agent joining the key below) does not bump this to v3: a v2 entry written before
# the fix has no "user-agent" key in request_header_values (only origin-Vary-listed headers
# were ever recorded), so under the new _match it can only ever compare "" against whatever
# User-Agent the current request actually carries — never a match by coincidence, since
# fetch_one always sends a real one. That resolves to the fallback this whole module is built
# on: a cache miss, safe by construction, never a wrong hit. v1 needed a version bump because a
# missing field would have been silently read as a fabricated value (size 0); here a
# schema-shaped-the-same entry simply fails the (now stricter) match and gets re-fetched, which
# is a slower first run per stale entry, not an incorrect one — no bump earns its cost.
#
# v3 (#352) added initial_age: the corrected initial age (RFC 9111 4.2.3) computed from the
# response's own ``Age``/``Date`` at store time, kept separate from ``stored_at`` (local
# receipt) so freshness is judged against ``initial_age + resident_time``, not resident time
# alone. A v2 entry has no such field, and defaulting it to 0.0 would silently read every
# pre-#352 disk entry as "just originated here" — exactly the bug this fix closes, just moved
# from computation into deserialization. So, like v1, v2 entries are not read: ``_read`` already
# discards anything whose ``schema_version`` does not match, which is a same-cost, slower-first-
# fetch fallback, never a wrong hit.
SCHEMA_VERSION = "http_cache.v3"
DEFAULT_DIR = "~/.cache/seohead/http_cache"

# Request headers folded into every entry's key regardless of what the origin names in Vary —
# see the module docstring's "User-Agent is always part of the key" paragraph. Kept as a tuple,
# not a single constant, in case a future crawler-controlled identity header joins it; today
# only User-Agent has that property (http.headers never reaches store()/decide() at all, see
# above).
CRAWLER_IDENTITY_HEADERS = ("user-agent",)

# A 304 has no payload. These fields therefore still describe the cached body, not the
# revalidation response, and replacing them would make a replayed PageRecord inconsistent with
# the bytes it parses. Other metadata (including Cache-Control, ETag, Vary and X-Robots-Tag) is
# current response metadata and must replace the stored value.
_REVALIDATION_PAYLOAD_HEADERS = frozenset(
    {
        "content-digest",
        "content-encoding",
        "content-language",
        "content-length",
        "content-location",
        "content-range",
        "content-type",
        "digest",
        "location",
        "repr-digest",
        "trailer",
        "transfer-encoding",
    }
)

# Stats counters, each incremented at exactly the moment its outcome is final. Total network
# round trips saved by the cache is ``hits + revalidations`` (a revalidation still costs one
# small request, but never re-transfers the body).
_STAT_KEYS = ("hits", "revalidations", "stores", "bypassed", "invalidated")


def resolve_dir() -> Path | None:
    """Where cache entries live, or ``None`` when disabled by environment.

    Mirrors ``seohead.runlog.log_path``: a well-known default, overridable with
    ``SEOHEAD_HTTP_CACHE_DIR``, set to ``off``/``0``/``none``/``false`` to disable outright.
    """
    override = os.environ.get("SEOHEAD_HTTP_CACHE_DIR")
    try:
        if override:
            if override.strip().lower() in ("off", "0", "none", "false"):
                return None
            return Path(override).expanduser()
        return Path(DEFAULT_DIR).expanduser()
    except (OSError, ValueError):
        return None


def _parse_cache_control(value: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, raw = part.partition("=")
            out[name.strip().lower()] = raw.strip().strip('"')
        else:
            out[part.lower()] = None
    return out


def freshness_lifetime(headers: dict[str, str]) -> tuple[float, bool]:
    """Seconds this response stays fresh, and whether it must never be stored at all.

    ``headers`` must already be lower-cased. Returns ``(0.0, True)`` for ``no-store``.
    Everything else that carries no usable freshness signal returns ``(0.0, False)``: store it
    (a validator may still save a round trip) but treat it as already stale — see the module
    docstring for why "unstated" is not read as "forever".
    """
    directives = _parse_cache_control(headers.get("cache-control", ""))
    if "no-store" in directives:
        return 0.0, True
    if "no-cache" in directives:
        return 0.0, False
    if directives.get("max-age") is not None:
        with contextlib.suppress(ValueError):
            return max(0.0, float(directives["max-age"])), False  # type: ignore[arg-type]
    expires = headers.get("expires")
    date_hdr = headers.get("date")
    if expires and date_hdr:
        with contextlib.suppress(ValueError, TypeError):
            expires_dt = parsedate_to_datetime(expires)
            base_dt = parsedate_to_datetime(date_hdr)
            if expires_dt is not None and base_dt is not None:
                return max(0.0, (expires_dt - base_dt).total_seconds()), False
    return 0.0, False


# RFC 9111 delta-seconds (used by ``Age``): ``1*DIGIT`` only — no sign, no decimal point, no
# leading/trailing junk. A sign or a decimal already reads as "not a delta-seconds", so
# ``"-5"`` and ``"5.0"`` are rejected the same way "banana" is, not parsed and then flipped.
_MAX_DELTA_SECONDS = 2**31 - 1  # RFC 9111 section 5.1: cap rather than overflow.


def _parse_delta_seconds(value: str) -> float | None:
    """Parse one ``delta-seconds`` value (``Age``'s grammar), or ``None`` if invalid.

    Section 5.1: a cache that cannot parse a field value as this grammar (or that produces a
    value larger than can be represented) MUST ignore it — never invent a number from it, and
    never let it shrink a corrected initial age below what ``Date`` alone would give.
    """
    value = value.strip()
    if not value or not value.isascii() or not value.isdigit():
        return None
    digits = value.lstrip("0") or "0"
    maximum = str(_MAX_DELTA_SECONDS)
    if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
        return float(_MAX_DELTA_SECONDS)
    return float(int(digits))


def corrected_initial_age(headers: dict[str, str], receipt_time: float) -> float:
    """RFC 9111 section 4.2.3's corrected initial age, without request/response round-trip
    timing (this cache does not plumb the request's send time through to ``store``/``refresh``,
    so ``response_delay`` is treated as 0 — the receipt instant stands in for both request_time
    and response_time). That still yields the two signals the section is built from:

    - ``apparent_age``: how much older the response's own ``Date`` says it already is than the
      moment this cache received it — this is what catches a stale intermediary whose ``Age``
      undersells the truth, or is missing outright.
    - the sender's own ``Age`` value (ignored if syntactically invalid — see
      ``_parse_delta_seconds``).

    The corrected initial age is the larger of the two, per the RFC: never let a small or absent
    ``Age`` undercut what ``Date`` already proves, and never let ``Date`` skew (clock drift
    between this machine and the origin) undercut a larger stated ``Age``.
    """
    apparent_age = 0.0
    date_hdr = headers.get("date")
    if date_hdr:
        with contextlib.suppress(ValueError, TypeError):
            date_dt = parsedate_to_datetime(date_hdr)
            if date_dt is not None:
                apparent_age = max(0.0, receipt_time - date_dt.timestamp())
    age_value = 0.0
    age_hdr = headers.get("age")
    if age_hdr is not None:
        parsed = _parse_delta_seconds(age_hdr)
        if parsed is not None:
            age_value = parsed
    return max(apparent_age, age_value)


@dataclass
class CacheEntry:
    """One stored representation of one URL."""

    url: str
    vary_headers: list[str] = field(default_factory=list)
    request_header_values: dict[str, str] = field(default_factory=dict)
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    # Bytes on the wire, after transfer decoding — not len(body). The two differ for anything
    # that is not valid UTF-8 (see issue #99), and a replayed page must report the size the
    # live fetch reported, not a size derived from its decoded text.
    size_bytes: int = 0
    stored_at: float = 0.0
    max_age: float = 0.0
    # Corrected initial age (RFC 9111 4.2.3) at the moment this cache received the response —
    # see ``corrected_initial_age``. 0.0 for an origin-fresh response with no ``Age``/stale
    # ``Date``; nonzero when the response arrived already partly aged (a shared proxy, a CDN).
    initial_age: float = 0.0

    @property
    def etag(self) -> str:
        return self.headers.get("etag", "")

    @property
    def last_modified(self) -> str:
        return self.headers.get("last-modified", "")

    def is_fresh(self, now: float) -> bool:
        """RFC 9111 4.2: fresh while ``freshness_lifetime > current_age``.

        ``current_age`` is the age already on the response when this cache received it, plus
        how long it has sat here since (the "resident time") — not just the resident time
        alone, which is what let an already-stale upstream response look brand new.
        """
        current_age = self.initial_age + (now - self.stored_at)
        return current_age < self.max_age


@dataclass
class CacheOutcome:
    """What a lookup decided to do, and what a revalidation request needs to send."""

    status: str  # "hit" | "revalidate" | "miss" | "bypass"
    entry: CacheEntry | None = None
    conditional_headers: dict[str, str] = field(default_factory=dict)


class ResponseCache:
    """A disk-backed HTTP cache for one crawl or collector run, safe under concurrency."""

    def __init__(
        self, directory: str | os.PathLike[str], *, mode: str = "live", invalidate: bool = False
    ) -> None:
        self.directory = Path(directory)
        self.mode = mode
        self.invalidate = invalidate
        self.stats: dict[str, int] = dict.fromkeys(_STAT_KEYS, 0)
        self._lock = threading.Lock()
        self._disabled = mode == "off"
        if not self._disabled:
            try:
                ensure_safe_dir(str(self.directory))
            except (PermissionError, OSError):
                # A cache that cannot be trusted degrades to "no cache", not to a crashed run:
                # every code path downstream already tolerates a plain miss.
                self._disabled = True

    def _bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def _family_dir(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.directory / digest[:2] / digest

    def _variants(self, url: str) -> list[CacheEntry]:
        out: list[CacheEntry] = []
        try:
            names = sorted(os.listdir(self._family_dir(url)))
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            entry = self._read(self._family_dir(url) / name)
            if entry is not None:
                out.append(entry)
        return out

    def _read(self, path: Path) -> CacheEntry | None:
        """Load one entry file. Never raises: a corrupt or hostile file is just a miss."""
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
                return None
            return CacheEntry(
                url=str(raw["url"]),
                vary_headers=[str(h) for h in raw.get("vary_headers") or []],
                request_header_values={
                    str(k): str(v) for k, v in (raw.get("request_header_values") or {}).items()
                },
                status_code=int(raw.get("status_code", 200)),
                headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
                body=str(raw.get("body", "")),
                size_bytes=int(raw.get("size_bytes", 0)),
                stored_at=float(raw.get("stored_at", 0.0)),
                max_age=float(raw.get("max_age", 0.0)),
                initial_age=float(raw.get("initial_age", 0.0)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _match(self, url: str, request_headers: dict[str, str]) -> CacheEntry | None:
        lowered = {k.lower(): v for k, v in request_headers.items()}
        for entry in self._variants(url):
            # Origin-declared Vary headers plus the crawler's own always-keyed identity
            # headers (User-Agent) — see CRAWLER_IDENTITY_HEADERS and the module docstring.
            keys = {h.lower() for h in entry.vary_headers} | set(CRAWLER_IDENTITY_HEADERS)
            if all(lowered.get(h, "") == entry.request_header_values.get(h, "") for h in keys):
                return entry
        return None

    def decide(self, url: str, request_headers: dict[str, str]) -> CacheOutcome:
        """Look up ``url`` and say what to do: serve it, revalidate it, or fetch it live."""
        if self._disabled:
            return CacheOutcome("bypass")
        entry = self._match(url, request_headers)
        if entry is None:
            return CacheOutcome("miss")
        if self.mode == "replay":
            self._bump("hits")
            return CacheOutcome("hit", entry)
        if self.invalidate:
            self._bump("invalidated")
            return CacheOutcome("miss")
        if entry.is_fresh(time.time()):
            self._bump("hits")
            return CacheOutcome("hit", entry)
        conditional: dict[str, str] = {}
        if entry.etag:
            conditional["If-None-Match"] = entry.etag
        if entry.last_modified:
            conditional["If-Modified-Since"] = entry.last_modified
        if conditional:
            return CacheOutcome("revalidate", entry, conditional)
        return CacheOutcome("miss")

    def refresh(self, entry: CacheEntry, response_headers: dict[str, str]) -> None:
        """Apply a 304's current metadata while retaining the stored response body."""
        if self._disabled:
            return
        response_headers = {name.lower(): value for name, value in response_headers.items()}
        headers = dict(entry.headers)
        headers.update(
            {
                name: value
                for name, value in response_headers.items()
                if name not in _REVALIDATION_PAYLOAD_HEADERS
            }
        )
        max_age, no_store = freshness_lifetime(headers)
        if no_store:
            self._forget(entry)
            self._bump("bypassed")
            return

        old_vary = {name.lower() for name in entry.vary_headers}
        vary_headers = [name.strip() for name in headers.get("vary", "").split(",") if name.strip()]
        new_vary = {name.lower() for name in vary_headers}
        receipt_time = time.time()
        entry.headers = headers
        entry.stored_at = receipt_time
        entry.max_age = max_age
        entry.initial_age = corrected_initial_age(headers, receipt_time)
        if headers.get("vary", "").strip() == "*" or new_vary != old_vary:
            # A new Vary selection needs request values that this entry did not record. Dropping
            # it is conservative: this revalidation still serves the confirmed body now, while
            # the next lookup fetches a representation under its new cache key.
            entry.vary_headers = vary_headers
            self._forget(entry)
        else:
            self._write(entry)
        self._bump("revalidations")

    def store(
        self,
        url: str,
        request_headers: dict[str, str],
        status_code: int,
        response_headers: dict[str, str],
        body: str,
        size_bytes: int = 0,
    ) -> None:
        """Record a fresh response, or explain in stats why it was not recorded."""
        if self._disabled:
            return
        headers = {k.lower(): v for k, v in response_headers.items()}
        if headers.get("vary", "").strip() == "*" or status_code >= 500:
            # Vary: * means "not safely reusable at all"; a 5xx is not a page worth replaying.
            self._bump("bypassed")
            return
        max_age, no_store = freshness_lifetime(headers)
        if no_store:
            self._bump("bypassed")
            return
        vary_headers = [h.strip() for h in headers.get("vary", "").split(",") if h.strip()]
        lowered_request = {k.lower(): v for k, v in request_headers.items()}
        # vary_headers itself stays a faithful record of what the origin declared; the values
        # actually recorded for matching also cover the crawler's own always-keyed headers, so
        # a later _match() sees User-Agent even when the origin never mentioned it.
        key_headers = {h.lower() for h in vary_headers} | set(CRAWLER_IDENTITY_HEADERS)
        receipt_time = time.time()
        entry = CacheEntry(
            url=url,
            vary_headers=vary_headers,
            request_header_values={h: lowered_request.get(h, "") for h in key_headers},
            status_code=status_code,
            headers=headers,
            body=body,
            size_bytes=size_bytes,
            stored_at=receipt_time,
            max_age=max_age,
            initial_age=corrected_initial_age(headers, receipt_time),
        )
        self._write(entry)
        self._bump("stores")

    def _entry_path(self, entry: CacheEntry) -> Path:
        variant_key = hashlib.sha256(
            json.dumps(sorted(entry.request_header_values.items()), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return self._family_dir(entry.url) / f"{variant_key}.json"

    def _write(self, entry: CacheEntry) -> None:
        """Write one entry, safe against another thread or process writing the same key.

        ``tempfile.mkstemp`` hands back a name no other caller can also receive, so two writers
        racing on the same variant never share one temp file (an earlier, shared-name ".tmp"
        scheme let a second writer's ``open(..., "w")`` truncate the first writer's still-open
        file underneath it — the concurrency bug this test file exists to catch). Whichever
        writer's ``os.replace`` lands second simply wins; the cache never observes a partial file.
        """
        path = self._entry_path(entry)
        payload = {"schema_version": SCHEMA_VERSION, **asdict(entry)}
        tmp_path: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_path, path)
            tmp_path = None
        except OSError:
            pass  # a cache that cannot write must not break the run it is trying to speed up
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)

    def _forget(self, entry: CacheEntry) -> None:
        with contextlib.suppress(OSError):
            os.remove(self._entry_path(entry))


def build(
    directory: str | os.PathLike[str] | None, *, mode: str = "live", invalidate: bool = False
) -> ResponseCache | None:
    """Construct a cache, or ``None`` when there is nowhere for it to live.

    A missing directory (environment override set to ``off``, or none resolvable) means "no
    cache", identically to ``mode="off"`` — the caller does not need to tell the two apart.
    """
    if directory is None or mode == "off":
        return None
    return ResponseCache(directory, mode=mode, invalidate=invalidate)
