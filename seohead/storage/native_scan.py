"""Transactional single-writer storage for unfinished native scan artifacts."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seohead.crawl.settings import (
    DEFAULTS,
)
from seohead.crawl.settings import (
    fingerprint as crawl_config_fingerprint,
)
from seohead.crawl.settings import (
    validate as validate_crawl_config,
)
from seohead.storage import (
    _PAGE_BOOLS,
    APPLICATION_ID,
    FORMAT_VERSION,
    MAX_RECORD_BYTES,
    USER_VERSION,
    ScanError,
    _config,
    _dump,
    _expected,
    _insert,
    _objects,
    _runtime,
    _schema,
    _url,
    _validate_scalar_storage,
)

try:  # Child C targets the supported macOS/Linux local-filesystem contract.
    import fcntl
except ImportError:  # pragma: no cover - platform contract, retained for a clear error.
    fcntl = None  # type: ignore[assignment]


_RUNTIME_KEYS = ("python", "sqlite", "httpx", "lxml", "beautifulsoup4")
_BODY_TABLES = ("bodies", "responses", "documents", "resource_refs")
_NATIVE_CONTEXT = {"native_commit", "robots_blocked_url"}
_LINK_KEYS = {
    "source",
    "destination",
    "anchor",
    "nofollow",
    "position",
    "rel",
    "target",
    "raw_href",
}
_FORM_KEYS = {"page", "method", "action", "has_password"}
_CURRENT_PAGE_INTS = {
    "size_bytes",
    "word_count",
    "content_frames",
    "content_frames_same_origin",
    "crawl_depth",
    "head_count",
    "body_count",
    "outlinks",
    "external_outlinks",
    "jsonld_blocks_found",
    "jsonld_blocks_parsed",
}
_OPTIONAL_PAGE_SOURCES = {
    "status_code",
    "response_time",
    "text_ratio",
    "title_outside_head",
    "meta_description_outside_head",
    "canonical_outside_head",
    "directives_outside_head",
    "hreflang_outside_head",
}
MAX_EDGES_PER_PAGE = 20_000
MAX_PAGE_COMMIT_ITEMS = 20_000
SNAPSHOT_RESERVE_BYTES = 1024 * 1024 * 1024
WAL_BACKPRESSURE_BYTES = 64 * 1024 * 1024
BACKUP_TIMEOUT_SECONDS = 60.0
FINALIZATION_TIMEOUT_SECONDS = 10.0
_NO_BODY_RETENTION = {
    "policy_version": "scan_retention.v1",
    "body_mode": "off",
    "max_body_bytes": 0,
    "max_body_store_bytes": 0,
    "min_free_bytes": 0,
    "history_warning_bytes": 0,
    "automatic_delete": False,
}


def _native_config(value: Any) -> dict[str, Any]:
    config = _config(value)

    def require_fields(actual, expected, path="config"):
        if not isinstance(actual, dict) or not expected.keys() <= actual.keys():
            raise ScanError(f"native {path} is not a complete resolved configuration")
        for name, default in expected.items():
            if isinstance(default, dict):
                require_fields(actual[name], default, f"{path}.{name}")

    require_fields(config, DEFAULTS)
    try:
        validate_crawl_config(config)
    except (TypeError, ValueError, KeyError) as exc:
        raise ScanError(f"native effective configuration is invalid: {exc}") from exc
    return config


@dataclass(frozen=True)
class Lease:
    url_id: int
    url: str
    depth: int
    queue_ordinal: int


@dataclass(frozen=True)
class CommitReceipt:
    evidence_revision: int
    already_committed: bool = False


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_chunks(value: Any, limit: int = MAX_RECORD_BYTES):
    # Bound scalars and nesting before JSONEncoder can allocate an escaped copy.
    # Iterator frames keep this walk bounded even when the caller supplies a list.
    stack = [iter((value,))]
    exhausted = object()
    raw_size = 0
    while stack:
        item = next(stack[-1], exhausted)
        if item is exhausted:
            stack.pop()
            continue
        raw_size += 1
        if isinstance(item, str):
            if len(item) > limit - raw_size:
                raise ScanError("native JSON payload exceeds its byte limit")
            raw_size += len(item.encode("utf-8"))
        elif isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ScanError("native JSON payload keys must be strings")
            stack.append(itertools.chain(item.keys(), item.values()))
        elif isinstance(item, (list, tuple)):
            stack.append(iter(item))
        elif item is not None and type(item) not in (bool, int, float):
            raise ScanError("native payload is not JSON-compatible")
        elif type(item) is float and not math.isfinite(item):
            raise ScanError("native JSON payload requires finite numbers")
        if raw_size > limit or len(stack) > 64:
            raise ScanError("native JSON payload exceeds its byte or nesting limit")
    size = 0
    encoder = json.JSONEncoder(
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    for part in encoder.iterencode(value):
        data = part.encode("utf-8")
        size += len(data)
        if size > limit:
            raise ScanError("native JSON payload exceeds its byte limit")
        yield data


def _digest(value: Any) -> str:
    digest = hashlib.sha256()
    for data in _json_chunks(value, 8 * MAX_RECORD_BYTES):
        digest.update(data)
    return digest.hexdigest()


def _bounded_items(
    values: Iterable[Any], label: str, limit: int = MAX_PAGE_COMMIT_ITEMS
) -> list[Any]:
    items = []
    size = 0
    for value in itertools.islice(values, limit + 1):
        size += sum(map(len, _json_chunks(value)))
        if len(items) == limit or size > MAX_RECORD_BYTES:
            raise ScanError(f"{label} exceeds the bounded per-page limit ({limit} items / 8 MiB)")
        items.append(value)
    return items


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class NativeScan:
    """One locked native scan writer; fetch workers must not own this object."""

    def __init__(self, path: Path, connection: sqlite3.Connection, lock_fd: int) -> None:
        self.path = path
        self.con = connection
        self._lock_fd = lock_fd
        self.failpoint: Callable[[str], None] | None = None

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        start_url: str,
        config: dict[str, Any],
        config_fingerprint: str | None = None,
        writer_version: str,
        writer_revision: str,
        runtime_versions: dict[str, str],
        limitations: Iterable[str] = (),
        retention: dict[str, Any] | None = None,
    ) -> NativeScan:
        """Create a no-clobber running scan with no audit and no body lanes."""
        _runtime()
        if fcntl is None:
            raise ScanError("native scan writer requires POSIX advisory file locking")
        if not start_url or not isinstance(start_url, str):
            raise ScanError("native scan start_url is required")
        if (
            type(writer_revision) is not str
            or len(writer_revision) != 40
            or any(ch not in "0123456789abcdef" for ch in writer_revision)
        ):
            raise ScanError("native scan writer_revision must be a lowercase 40-character Git SHA")
        if set(runtime_versions) != set(_RUNTIME_KEYS) or any(
            not isinstance(value, str) or not value for value in runtime_versions.values()
        ):
            raise ScanError("native scan requires complete runtime version provenance")
        effective = _native_config(config)
        derived_fingerprint = crawl_config_fingerprint(effective)
        if config_fingerprint is not None and config_fingerprint != derived_fingerprint:
            raise ScanError("native scan configuration fingerprint disagrees with effective config")
        target = Path(path).absolute()
        if os.path.lexists(target):
            raise ScanError(f"native scan output already exists: {target}")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".native-scan-", suffix=".sqlite", dir=target.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        con: sqlite3.Connection | None = None
        try:
            con = cls._connect_writer(temporary)
            con.executescript(_schema())
            now = _utc()
            policy = retention or _NO_BODY_RETENTION
            if policy != _NO_BODY_RETENTION:
                raise ScanError("child-C native scan requires the exact no-body retention policy")
            capabilities = {
                "pages": {
                    "state": "partial",
                    "reason": "native writer has no collector adapter yet",
                },
                "links": {
                    "state": "partial",
                    "reason": "native writer has no collector adapter yet",
                },
                "responses": {"state": "unavailable", "reason": "response capture is child G"},
                "html_bodies": {"state": "unavailable", "reason": "body capture is child G"},
                "rendered_bodies": {"state": "unavailable", "reason": "body capture is child G"},
                "resource_refs": {"state": "unavailable", "reason": "resource capture is child H"},
                "resource_bodies": {
                    "state": "unavailable",
                    "reason": "resource capture is child H",
                },
                "resume": {
                    "state": "partial",
                    "reason": "storage core only; collector adapter is child D",
                },
                "offline_reanalysis": {
                    "state": "unavailable",
                    "reason": "offline replay is child I",
                },
            }
            con.execute("BEGIN IMMEDIATE")
            _insert(
                con,
                "scan",
                {
                    "singleton": 1,
                    "scan_uuid": str(uuid.uuid4()),
                    "format_version": FORMAT_VERSION,
                    "evidence_version": "crawl.v1",
                    "writer_version": writer_version,
                    "writer_revision": writer_revision,
                    "runtime_versions_json": _dump(runtime_versions),
                    "created_at": now,
                    "finished_at": None,
                    "source_kind": "native",
                    "parent_scan_uuid": None,
                    "start_url": start_url,
                    "config_json": _dump(effective),
                    "config_fingerprint": derived_fingerprint,
                    "lifecycle": "running",
                    "finish_reason": "running",
                    "crawl_partial": 0,
                    "corpus_partial": 1,
                    "evidence_revision": 0,
                    "limitations_json": _dump(list(limitations)),
                    "capabilities_json": _dump(capabilities),
                    "retention_json": _dump(policy),
                    "pinned": 0,
                },
            )
            _insert(
                con,
                "resume_state",
                {
                    "singleton": 1,
                    "state_version": "scan_resume.v1",
                    "max_depth_reached": 0,
                    "elapsed_seconds": 0.0,
                    "circuit_timeout_streak": 0,
                    "circuit_server_error_streak": 0,
                    "crawl_delay_applied": None,
                    "throttle_state_json": _dump(
                        {
                            "schema_version": "scan_throttle.v1",
                            "delay_seconds": 0.0,
                            "concurrency": 1,
                            "consecutive_ok": 0,
                        }
                    ),
                },
            )
            cls._validate_native(con)
            con.commit()
            # The temporary name entered WAL while schema/header rows were
            # created. Publish only a checkpointed DELETE-journal main file:
            # linking the main file without its temporary-name WAL would lose
            # the committed header and retain an unusable journal mode.
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("PRAGMA journal_mode=DELETE")
            con.close()
            con = None
            _fsync_file(temporary)
            os.link(temporary, target, follow_symlinks=False)
            _fsync_directory(target.parent)
            temporary.unlink()
            return cls.open(target, expected_start_url=start_url, expected_config=effective)
        except FileExistsError as exc:
            raise ScanError(f"native scan output already exists: {target}") from exc
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise ScanError(f"cannot create native scan: {exc}") from exc
        finally:
            if con is not None:
                con.close()
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        expected_start_url: str | None = None,
        expected_config: dict[str, Any] | None = None,
    ) -> NativeScan:
        path = Path(path).absolute()
        if fcntl is None:
            raise ScanError("native scan writer requires POSIX advisory file locking")
        if not os.path.lexists(path) or path.is_symlink() or not path.is_file():
            raise ScanError("native scan writer requires an existing regular scan file")
        if path.stat().st_nlink != 1:
            raise ScanError("native scan writer refuses hard-linked database aliases")
        # SQLite itself owns byte-range locks on the database file.  A separate
        # advisory lock inode prevents two application writers without
        # contending with SQLite's own VFS locks (notably on macOS).
        lock_path = path.with_name(path.name + ".writer.lock")
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise ScanError(f"cannot acquire native scan writer lock: {exc}") from exc
        con: sqlite3.Connection | None = None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise ScanError("native scan writer lock must be a regular unaliased file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ScanError("native scan already has an active writer") from exc
            # This is intentionally before a rw connection: sqlite3.connect()
            # would create a missing file and PRAGMA journal_mode would mutate
            # a foreign file before its application/schema identity was known.
            scan = cls.inspect(path)["scan"]
            if scan["lifecycle"] in {"finished", "failed"}:
                raise ScanError(
                    f"native scan lifecycle {scan['lifecycle']!r} cannot be opened for writing"
                )
            if expected_start_url is not None and scan["start_url"] != expected_start_url:
                raise ScanError("native scan start URL differs; refusing unsafe resume")
            if expected_config is not None and scan[
                "config_fingerprint"
            ] != crawl_config_fingerprint(_native_config(expected_config)):
                raise ScanError("native scan configuration differs; refusing unsafe resume")
            con = cls._connect_writer(path)
            return cls(path, con, fd)
        except BaseException:
            if con is not None:
                con.close()
            os.close(fd)
            raise

    @classmethod
    def inspect(cls, path: str | Path, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Validate an unfinished native scan read-only; it is not a report reader."""
        _runtime()
        source = Path(path).resolve()
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ScanError("native inspection timeout must be positive")
        con: sqlite3.Connection | None = None
        try:
            con = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA trusted_schema=OFF")
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA foreign_keys=ON")
            deadline = time.monotonic() + timeout_seconds
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            con.execute("BEGIN")
            cls._validate_native(con)
            scan = dict(con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
            return {
                "scan": scan,
                "counts": {
                    table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    for table in (
                        "pages",
                        "links",
                        "forms",
                        "decisions",
                        "frontier",
                        "query_variants",
                    )
                },
            }
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise ScanError(f"cannot inspect native scan: {exc}") from exc
        finally:
            if con is not None:
                con.close()

    @staticmethod
    def _connect_writer(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(
            path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA cache_size=-8192")
        con.execute("PRAGMA temp_store=FILE")
        return con

    @staticmethod
    def _validate_native(con: sqlite3.Connection) -> None:
        if con.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ScanError("foreign application_id")
        if con.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION:
            raise ScanError("unsupported scan user_version")
        if _objects(con) != _expected()[0]:
            raise ScanError("scan.v1 schema differs")
        _validate_scalar_storage(con)
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ScanError("native scan failed quick_check")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ScanError("native scan has inconsistent foreign-key references")
        if con.execute("SELECT COUNT(*) FROM scan").fetchone()[0] != 1:
            raise ScanError("native scan requires exactly one header")
        scan = con.execute("SELECT * FROM scan WHERE singleton=1").fetchone()
        if scan["source_kind"] != "native" or scan["format_version"] != FORMAT_VERSION:
            raise ScanError("not a native scan.v1 artifact")
        if (
            scan["evidence_version"],
            scan["corpus_partial"],
            scan["parent_scan_uuid"],
            scan["pinned"],
        ) != ("crawl.v1", 1, None, 0):
            raise ScanError(
                "native evidence version, corpus completeness, parent or pin metadata is unsupported"
            )
        if scan["lifecycle"] not in {"running", "interrupted", "finished", "failed"}:
            raise ScanError("invalid native scan lifecycle")
        if con.execute("SELECT COUNT(*) FROM audit").fetchone()[0] != 0:
            raise ScanError("child-C native scan must not contain an audit")
        for table in _BODY_TABLES:
            if con.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None:
                raise ScanError(f"{table}: unavailable before child G/H")
        if con.execute("SELECT COUNT(*) FROM resume_state").fetchone()[0] != 1:
            raise ScanError("native scan requires one resume_state")
        try:
            config = _native_config(json.loads(scan["config_json"]))
            stored_fingerprint = crawl_config_fingerprint(config)
        except (TypeError, ValueError, KeyError) as exc:
            raise ScanError("native scan has invalid effective configuration") from exc
        if scan["config_fingerprint"] != stored_fingerprint:
            raise ScanError("native scan configuration fingerprint disagrees with effective config")
        try:
            capabilities = json.loads(scan["capabilities_json"])
            retention = json.loads(scan["retention_json"])
            limitations = json.loads(scan["limitations_json"])
        except (TypeError, ValueError) as exc:
            raise ScanError("native scan has invalid metadata JSON") from exc
        expected_capabilities = {
            "pages",
            "links",
            "responses",
            "html_bodies",
            "rendered_bodies",
            "resource_refs",
            "resource_bodies",
            "resume",
            "offline_reanalysis",
        }
        if (
            not isinstance(capabilities, dict)
            or set(capabilities) != expected_capabilities
            or any(
                not isinstance(value, dict)
                or set(value) != {"state", "reason"}
                or value["state"] not in {"complete", "partial", "unavailable"}
                or not isinstance(value["reason"], str)
                for value in capabilities.values()
            )
        ):
            raise ScanError("native scan has invalid capability metadata")
        if any(
            value["state"] != ("partial" if name in {"pages", "links", "resume"} else "unavailable")
            for name, value in capabilities.items()
        ):
            raise ScanError("native storage core capability state claims an unavailable feature")
        if retention != _NO_BODY_RETENTION or any(
            type(retention[key]) is not type(value) for key, value in _NO_BODY_RETENTION.items()
        ):
            raise ScanError("child-C retention must be the explicit no-body policy")
        if not isinstance(limitations, list) or any(
            not isinstance(value, str) for value in limitations
        ):
            raise ScanError("native scan limitations must be a string list")
        if (
            type(scan["scan_uuid"]) is not str
            or type(scan["writer_version"]) is not str
            or not scan["writer_version"]
            or type(scan["writer_revision"]) is not str
            or len(scan["writer_revision"]) != 40
            or any(ch not in "0123456789abcdef" for ch in scan["writer_revision"])
            or type(scan["runtime_versions_json"]) is not str
        ):
            raise ScanError("native scan UUID, writer version or writer revision is invalid")
        try:
            uuid.UUID(scan["scan_uuid"])
            versions = json.loads(scan["runtime_versions_json"])
        except (TypeError, ValueError) as exc:
            raise ScanError("native scan UUID or runtime provenance is invalid") from exc
        if (
            not isinstance(versions, dict)
            or set(versions) != set(_RUNTIME_KEYS)
            or any(type(value) is not str or not value for value in versions.values())
        ):
            raise ScanError("native scan runtime provenance is invalid")
        runtime = con.execute("SELECT * FROM resume_state WHERE singleton=1").fetchone()
        try:
            throttle = json.loads(runtime["throttle_state_json"])
        except (TypeError, ValueError) as exc:
            raise ScanError("native scan throttle state is invalid JSON") from exc
        if (
            not isinstance(throttle, dict)
            or set(throttle) != {"schema_version", "delay_seconds", "concurrency", "consecutive_ok"}
            or throttle["schema_version"] != "scan_throttle.v1"
            or type(throttle["concurrency"]) is not int
            or type(throttle["consecutive_ok"]) is not int
            or not isinstance(throttle["delay_seconds"], (int, float))
            or not math.isfinite(float(throttle["delay_seconds"]))
            or throttle["delay_seconds"] < 0
            or not 1 <= throttle["concurrency"] <= config["speed"]["concurrency"]
            or not 0 <= throttle["consecutive_ok"] <= 2
            or any(
                type(runtime[name]) is not int or runtime[name] < 0
                for name in (
                    "max_depth_reached",
                    "circuit_timeout_streak",
                    "circuit_server_error_streak",
                )
            )
            or not isinstance(runtime["elapsed_seconds"], (int, float))
            or not math.isfinite(float(runtime["elapsed_seconds"]))
            or runtime["elapsed_seconds"] < 0
            or (
                runtime["crawl_delay_applied"] is not None
                and (
                    not isinstance(runtime["crawl_delay_applied"], (int, float))
                    or not math.isfinite(float(runtime["crawl_delay_applied"]))
                    or runtime["crawl_delay_applied"] < 0
                )
            )
        ):
            raise ScanError("native scan runtime state is invalid")
        if con.execute(
            "SELECT 1 FROM frontier WHERE state NOT IN ('queued','inflight','done','excluded') LIMIT 1"
        ).fetchone():
            raise ScanError("native scan frontier has an invalid state")
        frontier_count, frontier_low, frontier_high = con.execute(
            "SELECT COUNT(*), MIN(queue_ordinal), MAX(queue_ordinal) FROM frontier"
        ).fetchone()
        if frontier_count and (frontier_low != 0 or frontier_high != frontier_count - 1):
            raise ScanError("native scan frontier queue ordinals are not a contiguous sequence")
        if con.execute("SELECT 1 FROM frontier WHERE depth < 0 LIMIT 1").fetchone():
            raise ScanError("native scan frontier depth cannot be negative")
        if (
            scan["lifecycle"] == "finished"
            and con.execute(
                "SELECT 1 FROM frontier WHERE state IN ('queued','inflight') LIMIT 1"
            ).fetchone()
        ):
            raise ScanError("terminal native scan has unfinished frontier work")
        for page in con.execute("SELECT * FROM pages"):
            frontier_page = con.execute(
                "SELECT depth FROM frontier WHERE url_id=?", (page["url_id"],)
            ).fetchone()
            if frontier_page is None or page["crawl_depth"] != frontier_page[0]:
                raise ScanError("native scan page crawl depth disagrees with its frontier lease")
            if any(
                page[name] is None
                for name in (
                    "content_frames",
                    "content_frames_same_origin",
                    "hreflang_json",
                    "body_unavailable",
                )
            ):
                raise ScanError("native scan page is missing a current PageRecord evidence field")
            if (
                page["size_bytes"] < 0
                or page["word_count"] < 0
                or page["crawl_depth"] < 0
                or page["content_frames"] < 0
                or page["content_frames_same_origin"] < 0
                or page["content_frames_same_origin"] > page["content_frames"]
                or page["body_unavailable"] not in (None, "", "oversized")
            ):
                raise ScanError("native scan page scalar/body marker is invalid")
            try:
                alternates = json.loads(page["hreflang_json"] or "[]")
                chain = json.loads(page["redirect_chain_json"])
            except (TypeError, ValueError) as exc:
                raise ScanError("native scan page JSON is invalid") from exc
            if (
                not isinstance(alternates, list)
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"lang", "raw_href", "url"}
                    or any(type(value) is not str for value in item.values())
                    for item in alternates
                )
                or not isinstance(chain, list)
                or any(not isinstance(item, dict) for item in chain)
            ):
                raise ScanError("native scan page hreflang or redirect chain is invalid")
        page_count, page_low, page_high = con.execute(
            "SELECT COUNT(*), MIN(page_ordinal), MAX(page_ordinal) FROM pages"
        ).fetchone()
        if page_count and (page_low != 0 or page_high != page_count - 1):
            raise ScanError("native scan page ordinals are not a contiguous sequence")
        if (
            scan["evidence_revision"] != page_count
            or con.execute(
                "SELECT 1 FROM frontier f LEFT JOIN pages p USING(url_id) "
                "WHERE (f.state='done') != (p.url_id IS NOT NULL) OR "
                "(p.url_id IS NOT NULL AND p.crawl_depth != f.depth) LIMIT 1"
            ).fetchone()
            or con.execute(
                "SELECT 1 FROM pages p LEFT JOIN frontier f USING(url_id) WHERE f.url_id IS NULL LIMIT 1"
            ).fetchone()
        ):
            raise ScanError("native page evidence revision or frontier completion/depth disagrees")
        if (
            con.execute("SELECT COUNT(*) FROM context_items WHERE kind='native_commit'").fetchone()[
                0
            ]
            != page_count
        ):
            raise ScanError("native scan is missing committed-page idempotency context")
        for row in con.execute(
            "SELECT source_document_id, evidence_representation, rel_json FROM links"
        ):
            if row["source_document_id"] is not None or row["evidence_representation"] != "static":
                raise ScanError("child-C links require static evidence without a document body")
            rel = json.loads(row["rel_json"])
            if not isinstance(rel, list) or any(type(token) is not str for token in rel):
                raise ScanError("native links.rel_json must be an ordered string list")
        for row in con.execute("SELECT source_document_id, evidence_representation FROM forms"):
            if row["source_document_id"] is not None or row["evidence_representation"] != "static":
                raise ScanError("child-C forms require static evidence without a document body")
        if con.execute(
            "SELECT 1 FROM links GROUP BY source_url_id, evidence_representation "
            "HAVING MIN(ordinal) != 0 OR MAX(ordinal) != COUNT(*) - 1 LIMIT 1"
        ).fetchone():
            raise ScanError("native scan link ordinals are not contiguous per source")
        if con.execute(
            "SELECT 1 FROM forms GROUP BY page_url_id, evidence_representation "
            "HAVING MIN(ordinal) != 0 OR MAX(ordinal) != COUNT(*) - 1 LIMIT 1"
        ).fetchone():
            raise ScanError("native scan form ordinals are not contiguous per source")
        for item in con.execute("SELECT * FROM context_items"):
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
                continue
            if item["kind"] != "robots_blocked_url":
                raise ScanError("native scan has an unsupported context kind")
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
            url = con.execute(
                "SELECT url FROM urls WHERE url_id=?", (payload["url_id"],)
            ).fetchone()
            if url is None:
                raise ScanError("native scan robots context references an unknown URL")
            if (
                payload["policy"] == "respect"
                and not con.execute(
                    "SELECT 1 FROM decisions WHERE url=? AND reason='blocked_by_robots'", (url[0],)
                ).fetchone()
            ):
                raise ScanError("native robots exclusion lacks its blocked_by_robots decision")

    def _assert_mutable(self) -> None:
        lifecycle = self.con.execute("SELECT lifecycle FROM scan WHERE singleton=1").fetchone()[0]
        if lifecycle in {"finished", "failed"}:
            raise ScanError(f"native scan lifecycle {lifecycle!r} is immutable")

    def close(self) -> None:
        if getattr(self, "con", None) is not None:
            self.con.close()
            self.con = None  # type: ignore[assignment]
        if getattr(self, "_lock_fd", None) is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
            os.close(self._lock_fd)
            self._lock_fd = None  # type: ignore[assignment]

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _hit(self, name: str) -> None:
        if self.failpoint is not None:
            self.failpoint(name)

    def _begin(self) -> None:
        self._enforce_wal_bound()
        self.con.execute("BEGIN IMMEDIATE")

    def _enforce_wal_bound(self) -> None:
        wal = self.path.with_name(self.path.name + "-wal")
        if not wal.exists() or wal.stat().st_size <= WAL_BACKPRESSURE_BYTES:
            return
        checkpoint = self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if wal.exists() and wal.stat().st_size > WAL_BACKPRESSURE_BYTES:
            busy, _log, _checkpointed = checkpoint
            raise ScanError(
                "WAL backpressure: checkpoint could not reduce the bounded log"
                + (" because a reader is active" if busy else "")
            )

    def _rollback(self) -> None:
        if self.con.in_transaction:
            self.con.rollback()

    def enqueue(self, entries: Iterable[tuple[str, int]]) -> list[Lease]:
        """Add only new frontier identities in one transaction."""
        self._assert_mutable()
        entries = _bounded_items(entries, "initial frontier entries")
        for url, depth in entries:
            if type(url) is not str or not url or type(depth) is not int or depth < 0:
                raise ScanError(
                    "frontier enqueue requires nonempty URL strings and nonnegative integer depths"
                )
        self._begin()
        try:
            next_ordinal = self.con.execute(
                "SELECT COALESCE(MAX(queue_ordinal)+1, 0) FROM frontier"
            ).fetchone()[0]
            added: list[Lease] = []
            for url, depth in entries:
                url_id = _url(self.con, url)
                existing = self.con.execute(
                    "SELECT state, queue_ordinal FROM frontier WHERE url_id=?", (url_id,)
                ).fetchone()
                if existing is not None:
                    continue
                self.con.execute(
                    "INSERT INTO frontier(url_id, queue_ordinal, depth, state) VALUES(?,?,?, 'queued')",
                    (url_id, next_ordinal, int(depth)),
                )
                added.append(Lease(url_id, url, int(depth), next_ordinal))
                next_ordinal += 1
            self.con.commit()
            return added
        except BaseException:
            self._rollback()
            raise

    def claim(self, limit: int) -> list[Lease]:
        if type(limit) is not int or limit < 1:
            raise ValueError("claim limit must be a positive integer")
        self._assert_mutable()
        config = json.loads(
            self.con.execute("SELECT config_json FROM scan WHERE singleton=1").fetchone()[0]
        )
        max_concurrency = config["speed"]["concurrency"]
        limit = max(
            0,
            min(
                limit,
                max_concurrency
                - self.con.execute(
                    "SELECT COUNT(*) FROM frontier WHERE state='inflight'"
                ).fetchone()[0],
            ),
        )
        self._begin()
        try:
            rows = list(
                self.con.execute(
                    "SELECT f.url_id, u.url, f.depth, f.queue_ordinal FROM frontier f "
                    "JOIN urls u USING(url_id) WHERE f.state='queued' ORDER BY f.queue_ordinal LIMIT ?",
                    (limit,),
                )
            )
            leases = [
                Lease(row["url_id"], row["url"], row["depth"], row["queue_ordinal"]) for row in rows
            ]
            self._hit("before_claim")
            self.con.executemany(
                "UPDATE frontier SET state='inflight' WHERE url_id=?", [(x.url_id,) for x in leases]
            )
            self.con.commit()
            self._hit("after_claim")
            return leases
        except BaseException:
            self._rollback()
            raise

    def recover_inflight(self) -> int:
        """Requeue only after this object obtained the exclusive lifetime lock."""
        self._assert_mutable()
        self._begin()
        try:
            count = self.con.execute(
                "SELECT COUNT(*) FROM frontier WHERE state='inflight'"
            ).fetchone()[0]
            self.con.execute("UPDATE frontier SET state='queued' WHERE state='inflight'")
            self.con.commit()
            return count
        except BaseException:
            self._rollback()
            raise

    def _page_row(self, record: dict[str, Any], lease: Lease) -> dict[str, Any]:
        if record.get("url") != lease.url:
            raise ScanError("page record URL differs from the claimed frontier lease")
        if record.get("crawl_depth") != lease.depth:
            raise ScanError("page record crawl depth differs from the claimed frontier lease")
        columns = {column[1]: column for column in _expected()[1]["pages"]}
        source_names = {"url"}
        for name in columns:
            if name not in {"url_id", "page_ordinal", "document_id"}:
                source_names.add(
                    {"redirect_chain_json": "redirect_chain", "hreflang_json": "hreflang"}.get(
                        name, name
                    )
                )
        if set(record) - source_names:
            raise ScanError(f"page record has unknown fields: {sorted(set(record) - source_names)}")
        for name in _CURRENT_PAGE_INTS:
            value = record.get(name)
            if type(value) is not int or value < 0:
                raise ScanError(f"pages.{name}: expected a nonnegative integer")
        if record.get("content_frames_same_origin", 0) > record.get("content_frames", 0):
            raise ScanError("pages.content_frames_same_origin exceeds content_frames")
        if record.get("body_unavailable") not in {"", "oversized"}:
            raise ScanError("pages.body_unavailable has an unknown marker")
        row: dict[str, Any] = {
            "url_id": lease.url_id,
            "page_ordinal": self.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
        }
        for name, column in columns.items():
            if name in {"url_id", "page_ordinal", "document_id"}:
                continue
            source = {"redirect_chain_json": "redirect_chain", "hreflang_json": "hreflang"}.get(
                name, name
            )
            if source not in record:
                raise ScanError(f"pages.{source}: current native record field is missing")
            value = record[source]
            if value is None and source not in _OPTIONAL_PAGE_SOURCES:
                raise ScanError(f"pages.{source}: current native record field cannot be null")
            if name in {"redirect_chain_json", "hreflang_json"}:
                if name == "hreflang_json" and (
                    not isinstance(value, list)
                    or any(
                        not isinstance(item, dict)
                        or set(item) != {"lang", "raw_href", "url"}
                        or any(type(part) is not str for part in item.values())
                        for item in value or []
                    )
                ):
                    raise ScanError(
                        "pages.hreflang must be ordered lang/raw_href/url string objects"
                    )
                if name == "redirect_chain_json" and (
                    not isinstance(value, list)
                    or any(not isinstance(item, dict) for item in value or [])
                ):
                    raise ScanError("pages.redirect_chain must be an ordered object list")
                value = _dump(value)
            elif name in _PAGE_BOOLS and value is not None:
                if type(value) is not bool:
                    raise ScanError(f"pages.{source}: expected bool or null")
                value = int(value)
            if value is None and column[3] and column[4] is None:
                raise ScanError(f"pages.{source}: required value is missing")
            if value is not None:
                row[name] = value
        for name, value in row.items():
            kind = columns[name][2]
            if kind == "TEXT" and type(value) is not str:
                raise ScanError(f"pages.{name}: expected text")
            if kind == "TEXT" and len(value.encode("utf-8")) > MAX_RECORD_BYTES:
                raise ScanError(f"pages.{name}: text exceeds the 8 MiB record limit")
            if kind == "INTEGER" and type(value) is not int:
                raise ScanError(f"pages.{name}: expected integer")
            if kind == "REAL" and (
                not isinstance(value, (int, float)) or not math.isfinite(float(value))
            ):
                raise ScanError(f"pages.{name}: expected finite real")
        return row

    def commit_page(
        self,
        lease: Lease,
        record: dict[str, Any],
        *,
        links: Iterable[dict[str, Any]] = (),
        forms: Iterable[dict[str, Any]] = (),
        decisions: Iterable[dict[str, Any]] = (),
        discovered: Iterable[tuple[str, int]] = (),
        query_reservations: Iterable[tuple[str, str, str]] = (),
        runtime: dict[str, Any] | None = None,
        context: Iterable[dict[str, Any]] = (),
    ) -> CommitReceipt:
        """Commit one complete fold-back unit or none of it."""
        self._assert_mutable()
        for _ in _json_chunks(record):
            pass
        links, forms, decisions, discovered, query_reservations, context = (
            _bounded_items(links, "links", MAX_EDGES_PER_PAGE),
            _bounded_items(forms, "forms"),
            _bounded_items(decisions, "decisions"),
            _bounded_items(discovered, "discovered frontier entries"),
            _bounded_items(query_reservations, "query reservations"),
            _bounded_items(context, "context items"),
        )
        payload = {
            "lease": lease.__dict__,
            "record": record,
            "links": links,
            "forms": forms,
            "decisions": decisions,
            "discovered": discovered,
            "query_reservations": query_reservations,
            "runtime": runtime or {},
            "context": context,
        }
        digest = _digest(payload)
        self._begin()
        try:
            frontier = self.con.execute(
                "SELECT f.url_id, u.url, f.depth, f.queue_ordinal, f.state FROM frontier f "
                "JOIN urls u USING(url_id) WHERE f.url_id=?",
                (lease.url_id,),
            ).fetchone()
            if frontier is None:
                raise ScanError("unknown frontier lease")
            if (
                frontier["url_id"],
                frontier["url"],
                frontier["depth"],
                frontier["queue_ordinal"],
            ) != (lease.url_id, lease.url, lease.depth, lease.queue_ordinal):
                raise ScanError("forged or stale frontier lease")
            if frontier["state"] == "done":
                prior = self.con.execute(
                    "SELECT payload_json FROM context_items WHERE kind='native_commit' AND item_key=?",
                    (str(lease.queue_ordinal),),
                ).fetchone()
                if prior is None or json.loads(prior["payload_json"]).get("digest") != digest:
                    raise ScanError("committed lease was retried with different evidence")
                revision = self.con.execute(
                    "SELECT evidence_revision FROM scan WHERE singleton=1"
                ).fetchone()[0]
                self.con.commit()
                return CommitReceipt(revision, already_committed=True)
            if frontier["state"] != "inflight" or frontier["queue_ordinal"] != lease.queue_ordinal:
                raise ScanError("frontier lease is no longer inflight")
            first_inflight = self.con.execute(
                "SELECT MIN(queue_ordinal) FROM frontier WHERE state='inflight'"
            ).fetchone()[0]
            if first_inflight != lease.queue_ordinal:
                raise ScanError("page commit must fold back the contiguous inflight prefix")
            _insert(self.con, "pages", self._page_row(record, lease))
            self._hit("after_page")
            for ordinal, item in enumerate(links):
                if set(item) != _LINK_KEYS:
                    raise ScanError("link input must have exactly the LinkEdge fields")
                if item["source"] != lease.url:
                    raise ScanError("link source differs from page lease")
                if (
                    any(
                        type(item[name]) is not str
                        for name in (
                            "source",
                            "destination",
                            "anchor",
                            "position",
                            "target",
                            "raw_href",
                        )
                    )
                    or not item["destination"]
                    or type(item["nofollow"]) is not bool
                    or not isinstance(item["rel"], (tuple, list))
                    or any(type(token) is not str for token in item["rel"])
                ):
                    raise ScanError("link input has invalid scalar types")
                row = {
                    "source_url_id": lease.url_id,
                    "destination_url_id": _url(self.con, item["destination"]),
                    "source_document_id": None,
                    "evidence_representation": "static",
                    "ordinal": ordinal,
                    "anchor": item["anchor"],
                    "nofollow": int(item["nofollow"]),
                    "position": item["position"],
                    "rel_json": _dump(list(item["rel"])),
                    "target": item["target"],
                    "raw_href": item["raw_href"],
                }
                _insert(self.con, "links", row)
            for ordinal, item in enumerate(forms):
                if set(item) != _FORM_KEYS:
                    raise ScanError("form input must have exactly the FormEdge fields")
                if item["page"] != lease.url:
                    raise ScanError("form page differs from page lease")
                if (
                    any(type(item[name]) is not str for name in ("page", "method", "action"))
                    or type(item["has_password"]) is not bool
                ):
                    raise ScanError("form input has invalid scalar types")
                _insert(
                    self.con,
                    "forms",
                    {
                        "page_url_id": lease.url_id,
                        "ordinal": ordinal,
                        "source_document_id": None,
                        "evidence_representation": "static",
                        "method": item["method"],
                        "action": item["action"],
                        "has_password": int(item["has_password"]),
                    },
                )
            self._hit("after_observations")
            for index, item in enumerate(decisions):
                allowed = {"url", "reason", "source", "depth", "occurrence_key"}
                if (
                    not isinstance(item, dict)
                    or set(item) - allowed
                    or not {"url", "reason", "source"} <= set(item)
                ):
                    raise ScanError("decision input has unknown or missing fields")
                if any(
                    type(item[name]) is not str or not item[name]
                    for name in ("url", "reason", "source")
                ):
                    raise ScanError("decision input has invalid string fields")
                if item.get("depth") is not None and (
                    type(item["depth"]) is not int or item["depth"] < 0
                ):
                    raise ScanError("decision input depth must be a nonnegative integer or null")
                if item.get("occurrence_key") is not None and (
                    type(item["occurrence_key"]) is not str or not item["occurrence_key"]
                ):
                    raise ScanError("decision occurrence key must be a nonempty string")
                _insert(
                    self.con,
                    "decisions",
                    {
                        "url": item["url"],
                        "reason": item["reason"],
                        "source": item["source"],
                        "depth": item.get("depth"),
                        "occurrence_key": item.get(
                            "occurrence_key", f"{lease.queue_ordinal}:decision:{index}"
                        ),
                    },
                )
            query_limit = self._stored_query_limit()
            for index, (path_key, query_key, requested_url) in enumerate(query_reservations):
                if not all(
                    isinstance(value, str) and value
                    for value in (path_key, query_key, requested_url)
                ):
                    raise ScanError("query reservation needs path, query and exact requested URL")
                count = self.con.execute(
                    "SELECT COUNT(*) FROM query_variants WHERE path_key=?", (path_key,)
                ).fetchone()[0]
                exists = self.con.execute(
                    "SELECT 1 FROM query_variants WHERE path_key=? AND query_key=?",
                    (path_key, query_key),
                ).fetchone()
                if not exists and query_limit > 0 and count >= query_limit:
                    self._hit("before_query_budget")
                    occurrence = f"{lease.queue_ordinal}:query_budget:{index}"
                    _insert(
                        self.con,
                        "decisions",
                        {
                            "url": requested_url,
                            "reason": "query_variants_limit",
                            "source": lease.url,
                            "depth": lease.depth + 1,
                            "occurrence_key": occurrence,
                        },
                    )
                    rejected_id = _url(self.con, requested_url)
                    if (
                        self.con.execute(
                            "SELECT 1 FROM frontier WHERE url_id=?", (rejected_id,)
                        ).fetchone()
                        is None
                    ):
                        self.con.execute(
                            "INSERT INTO frontier(url_id, queue_ordinal, depth, state) VALUES(?,?,?, 'excluded')",
                            (
                                rejected_id,
                                self.con.execute(
                                    "SELECT COALESCE(MAX(queue_ordinal)+1, 0) FROM frontier"
                                ).fetchone()[0],
                                lease.depth + 1,
                            ),
                        )
                    self._hit("after_query_budget")
                    continue
                self.con.execute(
                    "INSERT OR IGNORE INTO query_variants(path_key, query_key) VALUES(?,?)",
                    (path_key, query_key),
                )
                self._hit("after_query_budget")
            next_ordinal = self.con.execute(
                "SELECT COALESCE(MAX(queue_ordinal)+1, 0) FROM frontier"
            ).fetchone()[0]
            for url, depth in discovered:
                if type(url) is not str or not url or type(depth) is not int or depth < 0:
                    raise ScanError(
                        "discovered frontier entry must be a nonempty URL and nonnegative depth"
                    )
                url_id = _url(self.con, url)
                if (
                    self.con.execute("SELECT 1 FROM frontier WHERE url_id=?", (url_id,)).fetchone()
                    is None
                ):
                    self.con.execute(
                        "INSERT INTO frontier(url_id, queue_ordinal, depth, state) VALUES(?,?,?, 'queued')",
                        (url_id, next_ordinal, int(depth)),
                    )
                    next_ordinal += 1
            self.con.execute("UPDATE frontier SET state='done' WHERE url_id=?", (lease.url_id,))
            self._hit("after_frontier")
            self._hit("before_runtime")
            self._write_runtime(runtime or {}, lease.depth)
            self._hit("after_runtime")
            for item in context:
                if not isinstance(item, dict) or set(item) != {
                    "kind",
                    "item_key",
                    "payload_version",
                    "payload_json",
                    "completeness",
                    "reason",
                }:
                    raise ScanError("child-C context item has unknown or missing fields")
                if item["kind"] != "robots_blocked_url":
                    raise ScanError("child-C supports only documented robots_blocked_url context")
                if (
                    type(item["item_key"]) is not str
                    or item["payload_version"] != "scan_context.v1"
                    or item["completeness"] not in {"complete", "partial", "unavailable"}
                    or type(item["reason"]) is not str
                ):
                    raise ScanError("child-C robots context has invalid metadata")
                try:
                    payload = json.loads(item["payload_json"])
                except (TypeError, ValueError) as exc:
                    raise ScanError("child-C robots context payload is invalid JSON") from exc
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"url_id", "token", "policy"}
                    or type(payload["url_id"]) is not int
                    or payload["url_id"] <= 0
                    or item["item_key"] != f"url:{payload['url_id']}"
                    or type(payload["token"]) is not str
                    or payload["policy"] not in {"respect", "report_only"}
                ):
                    raise ScanError("child-C robots context must use the documented URL-id payload")
                url = self.con.execute(
                    "SELECT url FROM urls WHERE url_id=?", (payload["url_id"],)
                ).fetchone()
                if url is None:
                    raise ScanError("child-C robots context references an unknown URL")
                if (
                    payload["policy"] == "respect"
                    and not self.con.execute(
                        "SELECT 1 FROM decisions WHERE url=? AND reason='blocked_by_robots'",
                        (url[0],),
                    ).fetchone()
                ):
                    raise ScanError("child-C robots exclusion lacks blocked_by_robots decision")
                _insert(self.con, "context_items", item)
            _insert(
                self.con,
                "context_items",
                {
                    "kind": "native_commit",
                    "item_key": str(lease.queue_ordinal),
                    "payload_version": "scan_context.v1",
                    "payload_json": _dump({"digest": digest}),
                    "completeness": "complete",
                    "reason": "atomic page commit",
                },
            )
            self.con.execute(
                "UPDATE scan SET evidence_revision=evidence_revision+1 WHERE singleton=1"
            )
            self._hit("before_commit")
            self.con.commit()
            revision = self.con.execute(
                "SELECT evidence_revision FROM scan WHERE singleton=1"
            ).fetchone()[0]
            self._hit("after_commit")
            return CommitReceipt(revision)
        except BaseException:
            self._rollback()
            raise

    def _write_runtime(self, runtime: dict[str, Any], lease_depth: int) -> None:
        required = {
            "max_depth_reached",
            "elapsed_seconds",
            "circuit_timeout_streak",
            "circuit_server_error_streak",
            "crawl_delay_applied",
            "throttle",
        }
        if set(runtime) != required:
            raise ScanError("runtime state must have the exact child-C key set")
        throttle = runtime["throttle"]
        if set(throttle) != {"delay_seconds", "concurrency", "consecutive_ok"}:
            raise ScanError("runtime throttle state must have the exact child-C key set")
        config = _config(
            json.loads(
                self.con.execute("SELECT config_json FROM scan WHERE singleton=1").fetchone()[0]
            )
        )
        if (
            any(
                type(runtime[name]) is not int or runtime[name] < 0
                for name in (
                    "max_depth_reached",
                    "circuit_timeout_streak",
                    "circuit_server_error_streak",
                )
            )
            or not isinstance(runtime["elapsed_seconds"], (int, float))
            or not math.isfinite(float(runtime["elapsed_seconds"]))
            or runtime["elapsed_seconds"] < 0
            or (
                runtime["crawl_delay_applied"] is not None
                and (
                    not isinstance(runtime["crawl_delay_applied"], (int, float))
                    or not math.isfinite(float(runtime["crawl_delay_applied"]))
                    or runtime["crawl_delay_applied"] < 0
                )
            )
            or not isinstance(throttle["delay_seconds"], (int, float))
            or not math.isfinite(float(throttle["delay_seconds"]))
            or throttle["delay_seconds"] < 0
            or type(throttle["concurrency"]) is not int
            or not 1 <= throttle["concurrency"] <= config["speed"]["concurrency"]
            or type(throttle["consecutive_ok"]) is not int
            or not 0 <= throttle["consecutive_ok"] <= 2
        ):
            raise ScanError("runtime state has invalid finite values or throttle bounds")
        current = self.con.execute(
            "SELECT max_depth_reached, elapsed_seconds FROM resume_state WHERE singleton=1"
        ).fetchone()
        if runtime["max_depth_reached"] < max(current["max_depth_reached"], lease_depth):
            raise ScanError("runtime max depth must not regress below committed evidence")
        if runtime["elapsed_seconds"] < current["elapsed_seconds"]:
            raise ScanError("runtime elapsed seconds must not regress")
        values = (
            runtime["max_depth_reached"],
            runtime["elapsed_seconds"],
            runtime["circuit_timeout_streak"],
            runtime["circuit_server_error_streak"],
            runtime["crawl_delay_applied"],
            _dump({"schema_version": "scan_throttle.v1", **throttle}),
        )
        self.con.execute(
            "UPDATE resume_state SET max_depth_reached=?, elapsed_seconds=?, circuit_timeout_streak=?, "
            "circuit_server_error_streak=?, crawl_delay_applied=?, throttle_state_json=? WHERE singleton=1",
            values,
        )

    def _stored_query_limit(self) -> int:
        try:
            config = json.loads(
                self.con.execute("SELECT config_json FROM scan WHERE singleton=1").fetchone()[0]
            )
            limit = config["limits"]["max_query_variants_per_path"]
        except (TypeError, ValueError, KeyError) as exc:
            raise ScanError("stored effective config lacks a query-variant limit") from exc
        if type(limit) is not int or limit < 0:
            raise ScanError("stored query-variant limit is invalid")
        return limit

    def interrupt(self, reason: str) -> None:
        self._assert_mutable()
        self._begin()
        try:
            self.con.execute(
                "UPDATE scan SET lifecycle='interrupted', finish_reason=?, crawl_partial=1 WHERE singleton=1",
                (reason,),
            )
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

    def _finalize_checkpoint(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        prior_timeout = self.con.execute("PRAGMA busy_timeout").fetchone()[0]
        self.con.execute("PRAGMA busy_timeout=0")
        try:
            while True:
                busy, _log, _checkpointed = self.con.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if not busy:
                    try:
                        mode = self.con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                    except sqlite3.Error:
                        mode = ""
                    if str(mode).lower() == "delete":
                        return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        finally:
            self.con.execute(f"PRAGMA busy_timeout={prior_timeout}")

    def finish_without_audit(
        self,
        reason: str = "capture_finished_no_audit",
        *,
        timeout_seconds: float = FINALIZATION_TIMEOUT_SECONDS,
    ) -> bool:
        """Finish only after a bounded WAL checkpoint produces a DELETE-journal file."""
        self._assert_mutable()
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ScanError("native finalization timeout must be positive")
        # Finalization is the remedy for a large WAL, so ordinary writer
        # backpressure must not reject it before its checkpoint attempt.
        self.con.execute("BEGIN IMMEDIATE")
        try:
            if self.con.execute(
                "SELECT 1 FROM frontier WHERE state IN ('queued','inflight') LIMIT 1"
            ).fetchone():
                raise ScanError("cannot finish a scan with queued or inflight frontier work")
            self.con.rollback()
        except BaseException:
            self._rollback()
            raise
        if self._finalize_checkpoint(timeout_seconds):
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self.con.execute(
                    "UPDATE scan SET lifecycle='finished', finish_reason=?, finished_at=?, corpus_partial=1 WHERE singleton=1",
                    (reason, _utc()),
                )
                self.con.commit()
                return True
            except BaseException:
                self._rollback()
                raise
        # A reader blocked finalization. Retain the recoverable artifact but do
        # not advertise it as a finished one-file scan.
        try:
            self.con.execute("BEGIN IMMEDIATE")
            self.con.execute(
                "UPDATE scan SET lifecycle='interrupted', finish_reason='finalization_blocked' WHERE singleton=1"
            )
            self.con.commit()
        except BaseException:
            self._rollback()
            raise
        return False

    def resume_or_finalize(self) -> bool:
        """Requeue recovered work; an empty frontier has only finalization left to do."""
        self._assert_mutable()
        self.recover_inflight()
        if self.con.execute(
            "SELECT 1 FROM frontier WHERE state IN ('queued','inflight') LIMIT 1"
        ).fetchone():
            return False
        return self.finish_without_audit()

    def snapshot(
        self,
        destination: str | Path,
        *,
        reserve_bytes: int = SNAPSHOT_RESERVE_BYTES,
        temp_margin_bytes: int = 0,
        timeout_seconds: float = BACKUP_TIMEOUT_SECONDS,
        cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        target = Path(destination).absolute()
        if os.path.lexists(target):
            raise ScanError(f"snapshot target already exists: {target}")
        if (
            type(reserve_bytes) is not int
            or type(temp_margin_bytes) is not int
            or reserve_bytes < SNAPSHOT_RESERVE_BYTES
            or temp_margin_bytes < 0
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ScanError(
                "snapshot reserve, temporary margin and timeout must be bounded positive values"
            )
        page_count = self.con.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.con.execute("PRAGMA page_size").fetchone()[0]
        logical_size = page_count * page_size
        wal = self.path.with_name(self.path.name + "-wal")
        shm = self.path.with_name(self.path.name + "-shm")
        observed_margin = sum(path.stat().st_size for path in (wal, shm) if path.exists())
        required = (
            logical_size + observed_margin + max(0, reserve_bytes) + max(0, temp_margin_bytes)
        )
        free = os.statvfs(target.parent).f_bavail * os.statvfs(target.parent).f_frsize
        if free < required:
            raise ScanError(
                f"insufficient free space for native snapshot: need {required} bytes, have {free}"
            )
        fd, name = tempfile.mkstemp(prefix=".native-snapshot-", suffix=".sqlite", dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        dest: sqlite3.Connection | None = None
        try:
            dest = sqlite3.connect(temporary)
            deadline = time.monotonic() + timeout_seconds

            def progress(_status: int, _remaining: int, _total: int) -> None:
                if (cancelled is not None and cancelled()) or time.monotonic() > deadline:
                    raise ScanError("native snapshot cancelled or timed out")

            self.con.backup(dest, pages=128, progress=progress, sleep=0.01)
            dest.execute("PRAGMA journal_mode=DELETE")
            dest.close()
            dest = None
            _fsync_file(temporary)
            self.inspect(temporary)
            os.link(temporary, target, follow_symlinks=False)
            _fsync_directory(target.parent)
            return target
        except FileExistsError as exc:
            raise ScanError(f"snapshot target already exists: {target}") from exc
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ScanError(f"cannot snapshot native scan: {exc}") from exc
        finally:
            if dest is not None:
                dest.close()
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
