"""Transactional single-writer storage for unfinished native scan artifacts."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, is_dataclass
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
from seohead.storage.credential_context import (
    credential_verifier,
    redact_config,
    validate_recorded_credentials,
)
from seohead.storage.retention import policy_for_config, validate_policy

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


def _native_config(value: Any, *, recorded: bool = False) -> dict[str, Any]:
    config = _config(value if recorded else redact_config(value))

    def require_fields(actual, expected, path="config"):
        if not isinstance(actual, dict) or not expected.keys() <= actual.keys():
            raise ScanError(f"native {path} is not a complete resolved configuration")
        for name, default in expected.items():
            if isinstance(default, dict):
                require_fields(actual[name], default, f"{path}.{name}")

    # Earlier native v1 captures predate the optional storage settings. Validate
    # their recorded configuration without filling it or changing its fingerprint.
    require_fields(
        config,
        {k: v for k, v in DEFAULTS.items() if k not in {"storage", "resources"} or k in config},
    )
    try:
        validate_crawl_config(validate_recorded_credentials(config) if recorded else value)
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
        initial_sitemaps: Iterable[tuple[str, str]] = (),
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
        initial_sitemaps = _bounded_items(initial_sitemaps, "selected sitemap roots", 5000)
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
            scan_uuid = str(uuid.uuid4())
            policy = (
                validate_policy(retention)
                if retention is not None
                else policy_for_config(effective)
            )
            if policy != policy_for_config(effective):
                raise ScanError("body retention differs from the effective storage configuration")
            capabilities = {
                "pages": {
                    "state": "partial",
                    "reason": "native writer has no collector adapter yet",
                },
                "links": {
                    "state": "partial",
                    "reason": "native writer has no collector adapter yet",
                },
                "responses": {"state": "unavailable", "reason": "no response observations yet"},
                "html_bodies": {"state": "unavailable", "reason": "no retained HTML bodies yet"},
                "rendered_bodies": {
                    "state": "unavailable",
                    "reason": "no retained rendered DOM yet",
                },
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
                    "reason": "no retained offline reanalysis inputs yet",
                },
            }
            con.execute("BEGIN IMMEDIATE")
            _insert(
                con,
                "scan",
                {
                    "singleton": 1,
                    "scan_uuid": scan_uuid,
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
            if "storage" in effective:
                _insert(
                    con,
                    "context_items",
                    {
                        "kind": "credential_context",
                        "item_key": "run",
                        "payload_version": "scan_context.v1",
                        "payload_json": _dump(
                            {
                                "verifier": credential_verifier(config, scan_uuid),
                                "implicit_state": bool(
                                    config["rendering"]["browser"]["persistent_profile"]
                                ),
                            }
                        ),
                        "completeness": "complete",
                        "reason": "",
                    },
                )
            from .sitemaps import declare

            for ordinal, (sitemap_url, source) in enumerate(initial_sitemaps):
                declare(con, sitemap_url, source, ordinal)
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
            return cls.open(target, expected_start_url=start_url, expected_config=config)
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
        expected_writer_revision: str | None = None,
        _allow_reanalysis: bool = False,
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
            if scan["source_kind"] == "reanalysis" and not _allow_reanalysis:
                raise ScanError("derived reanalysis artifacts cannot resume collection")
            if scan["lifecycle"] in {"finished", "failed"}:
                raise ScanError(
                    f"native scan lifecycle {scan['lifecycle']!r} cannot be opened for writing"
                )
            if expected_start_url is not None and scan["start_url"] != expected_start_url:
                raise ScanError("native scan start URL differs; refusing unsafe resume")
            if (
                expected_writer_revision is not None
                and scan["writer_revision"] != expected_writer_revision
            ):
                raise ScanError("native scan producing build differs; refusing mixed-build resume")
            if expected_config is not None and scan[
                "config_fingerprint"
            ] != crawl_config_fingerprint(_native_config(expected_config)):
                raise ScanError("native scan configuration differs; refusing unsafe resume")
            # Credential references/values never enter the artifact. A local,
            # per-scan verifier detects a changed explicit context on resume.
            if expected_config is not None:
                from . import open_scan

                with open_scan(path, require_audit=False) as reader:
                    row = reader.execute(
                        "SELECT payload_json FROM context_items WHERE kind='credential_context' AND item_key='run'"
                    ).fetchone()
                    if row is not None:
                        recorded = json.loads(row[0])
                        observed = reader.execute("SELECT 1 FROM responses LIMIT 1").fetchone()
                        if recorded["implicit_state"] and observed:
                            raise ScanError(
                                "implicit cookie or browser credential state cannot be restored; refusing unsafe resume"
                            )
                        if recorded["verifier"] != credential_verifier(
                            expected_config, scan["scan_uuid"]
                        ):
                            raise ScanError("credential context differs; refusing unsafe resume")
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
        if (
            scan["source_kind"] not in {"native", "reanalysis"}
            or scan["format_version"] != FORMAT_VERSION
        ):
            raise ScanError("not a native scan.v1 artifact")
        if scan["evidence_version"] != "crawl.v1" or scan["pinned"] != 0:
            raise ScanError(
                "native evidence version, corpus completeness, parent or pin metadata is unsupported"
            )
        if scan["source_kind"] == "native" and scan["parent_scan_uuid"] is not None:
            raise ScanError("native metadata: capture cannot name a parent scan")
        if scan["source_kind"] == "reanalysis":
            try:
                uuid.UUID(scan["parent_scan_uuid"])
            except (TypeError, ValueError) as exc:
                raise ScanError("derived reanalysis requires a parent scan UUID") from exc
        if scan["lifecycle"] not in {"running", "interrupted", "finished", "failed"}:
            raise ScanError("invalid native scan lifecycle")
        from .native_audit import validate_audit

        validate_audit(con, scan)
        if con.execute("SELECT COUNT(*) FROM resume_state").fetchone()[0] != 1:
            raise ScanError("native scan requires one resume_state")
        try:
            config = _native_config(json.loads(scan["config_json"]), recorded=True)
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
            value["state"]
            not in (
                {"partial", "complete"}
                if name in {"pages", "links"}
                else {"unavailable"}
                if name == "resume" and scan["source_kind"] == "reanalysis"
                else {"partial", "complete"}
                if name == "resume"
                else {"complete"}
                if name == "offline_reanalysis" and scan["source_kind"] == "reanalysis"
                else {"partial", "complete", "unavailable"}
                if name in {"responses", "html_bodies", "rendered_bodies", "offline_reanalysis"}
                or ("resources" in config and name in {"resource_refs", "resource_bodies"})
                else {"unavailable"}
            )
            for name, value in capabilities.items()
        ):
            raise ScanError("native storage core capability state claims an unavailable feature")
        # Older G/H files honestly recorded that their writer did not expose
        # reanalysis. Reading them never upgrades that saved capability.
        if (
            capabilities["offline_reanalysis"]["state"] != "unavailable"
            and scan["source_kind"] == "native"
            and capabilities["offline_reanalysis"]["state"] != _reanalysis_capability(con)["state"]
        ):
            raise ScanError("offline reanalysis capability disagrees with retained inputs")
        validate_policy(retention)
        if retention != policy_for_config(config):
            raise ScanError("native retention policy disagrees with its recorded configuration")
        from .corpus_validation import validate_corpus

        validate_corpus(con, dict(scan), retention)
        if "storage" in config:
            credential = con.execute(
                "SELECT payload_json FROM context_items WHERE kind='credential_context' AND item_key='run'"
            ).fetchone()
            if credential is None:
                raise ScanError("native scan is missing its credential resume context")
            if con.execute("SELECT 1 FROM pages LIMIT 1").fetchone():
                from .corpus import corpus_summary

                coverage = corpus_summary(con, retention)
                if bool(scan["corpus_partial"]) != coverage["corpus_partial"] or any(
                    capabilities[name]["state"] != value["state"]
                    for name, value in coverage["capabilities"].items()
                ):
                    raise ScanError(
                        "native corpus capability metadata disagrees with recorded observations"
                    )
        if (
            not scan["corpus_partial"]
            and con.execute("SELECT 1 FROM bodies LIMIT 1").fetchone() is None
        ):
            raise ScanError("native corpus metadata claims complete bytes without a retained body")
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
            and scan["source_kind"] == "native"
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
        evidence_floor = (
            page_count
            + con.execute(
                "SELECT COUNT(*) FROM context_items WHERE kind='resource_commit'"
            ).fetchone()[0]
            + con.execute(
                "SELECT COUNT(*) FROM documents WHERE representation IN ('rendered','legacy_fragment')"
            ).fetchone()[0]
        )
        provenance = con.execute(
            "SELECT payload_json FROM context_items WHERE kind='reanalysis_provenance' AND item_key='run'"
        ).fetchone()
        derived_revision = None
        if provenance is not None:
            try:
                derived_revision = json.loads(provenance[0]).get("derived_evidence_revision")
            except (TypeError, ValueError):
                derived_revision = None
        revision_invalid = (
            scan["evidence_revision"] != evidence_floor
            if scan["source_kind"] == "native"
            else provenance is None
            or scan["evidence_revision"] != derived_revision
            or scan["evidence_revision"] < evidence_floor
        )
        if (
            revision_invalid
            or con.execute(
                "SELECT 1 FROM frontier f LEFT JOIN pages p USING(url_id) "
                "WHERE (f.state='done') != (p.url_id IS NOT NULL) OR "
                "(p.url_id IS NOT NULL AND p.crawl_depth != f.depth) LIMIT 1"
            ).fetchone()
            or con.execute(
                "SELECT 1 FROM pages p LEFT JOIN frontier f USING(url_id) WHERE f.url_id IS NULL LIMIT 1"
            ).fetchone()
        ):
            if scan["source_kind"] == "reanalysis":
                raise ScanError("derived reanalysis provenance or revision disagrees")
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
            if row["evidence_representation"] not in {"static", "rendered", "legacy_fragment"}:
                raise ScanError("native link representation is invalid")
            rel = json.loads(row["rel_json"])
            if not isinstance(rel, list) or any(type(token) is not str for token in rel):
                raise ScanError("native links.rel_json must be an ordered string list")
        for row in con.execute("SELECT source_document_id, evidence_representation FROM forms"):
            if row["evidence_representation"] not in {"static", "rendered", "legacy_fragment"}:
                raise ScanError("native form representation is invalid")
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
        from .sitemaps import root_ids

        sitemap_roots = root_ids(con)
        for item in con.execute("SELECT * FROM context_items"):
            from .native_context import validate_context

            validate_context(con, dict(item), sitemap_roots=sitemap_roots)
        if scan["source_kind"] == "reanalysis":
            marker = con.execute(
                "SELECT payload_json FROM context_items WHERE kind='reanalysis_provenance' AND item_key='run'"
            ).fetchone()
            if (
                marker is None
                or json.loads(marker[0])["parent_scan_uuid"] != scan["parent_scan_uuid"]
            ):
                raise ScanError("derived reanalysis provenance is missing or disagrees")
        for kind, expression in (
            ("resource_commit", "CAST(substr(item_key,10) AS INTEGER)"),
            ("sitemap_declaration", "CAST(substr(item_key,9) AS INTEGER)"),
            (
                "sitemap_declared_url",
                "CAST(substr(item_key,instr(item_key,':ordinal:')+9) AS INTEGER)",
            ),
        ):
            count, unique, low, high = con.execute(
                f"SELECT COUNT(*),COUNT(DISTINCT {expression}),MIN({expression}),MAX({expression}) FROM context_items WHERE kind=?",
                (kind,),
            ).fetchone()
            if count and (unique != count or low != 0 or high != count - 1):
                raise ScanError(f"{kind}: source ordinals are not one contiguous run-wide sequence")

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
        self.con.execute("DELETE FROM audit")

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

    def resume_snapshot(self, *, include_edges: bool = False) -> dict[str, Any]:
        """Read only scalar resume state; complete edge counts are opt-in."""
        scan = dict(self.con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
        runtime = dict(self.con.execute("SELECT * FROM resume_state WHERE singleton=1").fetchone())
        throttle = json.loads(runtime.pop("throttle_state_json"))
        throttle.pop("schema_version")
        runtime["throttle"] = throttle
        runtime.pop("singleton")
        runtime.pop("state_version")
        counts = {"pages": self.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]}
        counts.update(
            {
                row[0]: row[1]
                for row in self.con.execute("SELECT state,COUNT(*) FROM frontier GROUP BY state")
            }
        )
        for state in ("queued", "inflight", "done", "excluded"):
            counts.setdefault(state, 0)
        if include_edges:
            for table in ("links", "forms", "decisions"):
                counts[table] = self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {"scan": scan, "runtime": runtime, "counts": counts}

    def read_context(self, kind: str, key: str = "run"):
        row = self.con.execute(
            "SELECT payload_json FROM context_items WHERE kind=? AND item_key=?", (kind, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _sync_corpus(self) -> None:
        """Update declared corpus availability in the evidence transaction."""
        from .corpus import corpus_summary

        row = self.con.execute(
            "SELECT capabilities_json,retention_json,source_kind FROM scan"
        ).fetchone()
        capabilities = json.loads(row[0])
        summary = corpus_summary(self.con, json.loads(row[1]))
        capabilities.update(summary["capabilities"])
        if row[2] == "native":
            capabilities["offline_reanalysis"] = _reanalysis_capability(self.con)
        self.con.execute(
            "UPDATE scan SET capabilities_json=?,corpus_partial=? WHERE singleton=1",
            (_dump(capabilities), int(summary["corpus_partial"])),
        )

    def declare_sitemap(self, url: str, source: str, ordinal: int) -> int:
        from .sitemaps import declare

        self._assert_mutable()
        for _ in _json_chunks({"url": url, "source": source, "ordinal": ordinal}):
            pass
        self._begin()
        try:
            sid = declare(self.con, url, source, ordinal)
            self.con.commit()
            return sid
        except BaseException:
            self._rollback()
            raise

    def sitemap_roots(self) -> list[dict[str, Any]]:
        roots = []
        for row in self.con.execute(
            "SELECT payload_json FROM context_items WHERE kind='sitemap_declaration'"
        ):
            if len(roots) >= 5000:
                raise ScanError("too many selected sitemap roots")
            value = json.loads(row[0])
            value["url"] = self.con.execute(
                "SELECT url FROM urls WHERE url_id=?", (value["sitemap_url_id"],)
            ).fetchone()[0]
            roots.append(value)
        return sorted(roots, key=lambda value: value["ordinal"])

    def iter_sitemap_members(self, sid: int):
        for row in self.con.execute(
            "SELECT payload_json FROM context_items WHERE kind='sitemap_declared_url' AND item_key LIKE ? ORDER BY CAST(substr(item_key,instr(item_key,':ordinal:')+9) AS INTEGER)",
            (f"sitemap:{sid}:ordinal:%",),
        ):
            payload = json.loads(row[0])
            url = self.con.execute(
                "SELECT url FROM urls WHERE url_id=?", (payload["url_id"],)
            ).fetchone()[0]
            yield payload["ordinal"], url

    def write_sitemap_members(self, sid: int, entries) -> None:
        from .native_context import put_context
        from .sitemaps import _context, root_ids

        self._assert_mutable()
        entries = _bounded_items(entries, "sitemap membership chunk", 256)
        roots = root_ids(self.con)
        self._begin()
        try:
            next_ordinal = self.con.execute(
                "SELECT COUNT(*) FROM context_items WHERE kind='sitemap_declared_url'"
            ).fetchone()[0]
            for ordinal, url in entries:
                if type(ordinal) is not int or ordinal < 0 or type(url) is not str or not url:
                    raise ScanError("sitemap member requires a source ordinal and URL")
                key = f"sitemap:{sid}:ordinal:{ordinal}"
                exists = self.con.execute(
                    "SELECT 1 FROM context_items WHERE kind='sitemap_declared_url' AND item_key=?",
                    (key,),
                ).fetchone()
                if exists is None:
                    if ordinal != next_ordinal:
                        raise ScanError("sitemap member order changed during capture/resume")
                    next_ordinal += 1
                put_context(
                    self.con,
                    _context(
                        "sitemap_declared_url",
                        key,
                        {
                            "sitemap_url_id": sid,
                            "url_id": _url(self.con, url),
                            "ordinal": ordinal,
                        },
                        reason="selected sitemap expansion membership",
                    ),
                    sitemap_roots=roots,
                )
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

    def finish_sitemap(self, sid: int, complete: bool, reason: str) -> None:
        from .sitemaps import finish

        self._assert_mutable()
        for _ in _json_chunks({"sitemap_url_id": sid, "complete": complete, "reason": reason}):
            pass
        self._begin()
        try:
            finish(self.con, sid, complete, reason)
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

    def begin_collection(self) -> None:
        """Declare the delivered collector and reset only recoverable interruption state."""
        self._assert_mutable()
        self._begin()
        try:
            row = self.con.execute(
                "SELECT limitations_json,capabilities_json FROM scan WHERE singleton=1"
            ).fetchone()
            limitations = json.loads(row[0])
            partial = any(
                reason.partition(":")[0]
                in {
                    "link_observations_omitted",
                    "form_observations_omitted",
                    "response_body_unavailable",
                    "resource_declarations_omitted",
                }
                for reason in limitations
            )
            partial = (
                partial
                or self.con.execute(
                    "SELECT (SELECT COUNT(*) FROM context_items WHERE kind='sitemap_declaration') "
                    "!= (SELECT COUNT(*) FROM context_items WHERE kind='sitemap_fetch_summary' AND completeness='complete')"
                ).fetchone()[0]
            )
            capabilities = json.loads(row[1])
            for kind in ("pages", "links"):
                capabilities[kind] = {
                    "state": "partial" if partial else "complete",
                    "reason": "native observation prefix omitted"
                    if partial
                    else "committed native crawl observations",
                }
            capabilities["resume"] = {
                "state": "complete",
                "reason": "native frontier and runtime state retained",
            }
            self.con.execute(
                "UPDATE scan SET lifecycle='running',finish_reason='running',finished_at=NULL,crawl_partial=?,capabilities_json=? WHERE singleton=1",
                (int(partial), _dump(capabilities)),
            )
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

    def note_audit_unavailable(self, reason: str) -> None:
        """Keep a missing analyzer result distinct from incomplete collection."""
        self._assert_mutable()
        if type(reason) is not str or not reason or len(reason) > 500:
            raise ScanError("audit availability reason must be a short nonempty string")
        self._begin()
        try:
            notes = json.loads(
                self.con.execute("SELECT limitations_json FROM scan WHERE singleton=1").fetchone()[
                    0
                ]
            )
            note = "audit unavailable: " + reason
            if note not in notes:
                if len(notes) >= 64:
                    raise ScanError("native scan limitation registry is full")
                notes.append(note)
            self.con.execute(
                "UPDATE scan SET limitations_json=? WHERE singleton=1", (_dump(notes),)
            )
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

    def write_context(self, items) -> None:
        from .native_context import put_context

        self._assert_mutable()
        items = _bounded_items(items, "context")
        self._begin()
        try:
            for item in items:
                put_context(self.con, item)
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

    def seed_frontier(self, entries) -> dict[str, int]:
        from .frontier import apply_seeds

        self._assert_mutable()
        entries = _bounded_items(entries, "initial seeds")
        self._begin()
        try:
            start = self.con.execute("SELECT start_url FROM scan WHERE singleton=1").fetchone()[0]
            counts = apply_seeds(
                self.con, entries, limit=self._stored_query_limit(), start_url=start
            )
            self.con.commit()
            return counts
        except BaseException:
            self._rollback()
            raise

    def _partial_reasons(self, reasons) -> None:
        if not reasons:
            return
        if len(reasons) > 16 or any(
            type(reason) is not str or not reason or len(reason) > 200 for reason in reasons
        ):
            raise ScanError("partial reasons must be a finite short string list")
        row = self.con.execute(
            "SELECT limitations_json,capabilities_json FROM scan WHERE singleton=1"
        ).fetchone()
        limitations = json.loads(row[0])
        for reason in reasons:
            if reason not in limitations:
                if len(limitations) >= 64:
                    raise ScanError("native scan limitation registry is full")
                limitations.append(reason)
        capabilities = json.loads(row[1])
        for kind in ("pages", "links"):
            capabilities[kind] = {"state": "partial", "reason": "; ".join(reasons)}
        self.con.execute(
            "UPDATE scan SET crawl_partial=1, limitations_json=?, capabilities_json=? WHERE singleton=1",
            (_dump(limitations), _dump(capabilities)),
        )

    def exclude_lease(self, lease: Lease, reason: str, *, runtime, context=()) -> None:
        from .native_context import put_context

        self._assert_mutable()
        if type(reason) is not str or not reason or len(reason) > 200:
            raise ScanError("exclusion reason must be a short nonempty string")
        context = _bounded_items(context, "exclusion context")
        self._begin()
        try:
            row = self.con.execute(
                "SELECT f.*,u.url FROM frontier f JOIN urls u USING(url_id) WHERE f.url_id=?",
                (lease.url_id,),
            ).fetchone()
            if row is None or (row["url"], row["depth"], row["queue_ordinal"], row["state"]) != (
                lease.url,
                lease.depth,
                lease.queue_ordinal,
                "inflight",
            ):
                raise ScanError("exclusion requires the exact inflight lease")
            if (
                self.con.execute(
                    "SELECT MIN(queue_ordinal) FROM frontier WHERE state='inflight'"
                ).fetchone()[0]
                != lease.queue_ordinal
            ):
                raise ScanError("exclusion must preserve the contiguous inflight prefix")
            _insert(
                self.con,
                "decisions",
                {
                    "url": lease.url,
                    "reason": reason,
                    "source": "frontier",
                    "depth": lease.depth,
                    "occurrence_key": f"lease:{lease.queue_ordinal}:excluded",
                },
            )
            self.con.execute("UPDATE frontier SET state='excluded' WHERE url_id=?", (lease.url_id,))
            self._write_runtime(runtime, 0)
            for item in context:
                put_context(self.con, item)
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

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
        if (
            self.con.execute("SELECT 1 FROM frontier WHERE state='inflight' LIMIT 1").fetchone()
            is None
        ):
            return 0
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

    def _write_observations(self, lease, document_id, representation, links, forms):
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
                "source_document_id": document_id,
                "evidence_representation": representation,
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
                    "source_document_id": document_id,
                    "evidence_representation": representation,
                    "method": item["method"],
                    "action": item["action"],
                    "has_password": int(item["has_password"]),
                },
            )

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
        candidates: Iterable[dict[str, Any]] = (),
        partial_reasons: Iterable[str] = (),
        runtime: dict[str, Any] | None = None,
        context: Iterable[dict[str, Any]] = (),
        captures: Iterable[Any] = (),
        resources: Iterable[dict[str, Any]] = (),
        resource_inventory_state: str | None = None,
        resources_omitted: int = 0,
    ) -> CommitReceipt:
        """Commit one complete fold-back unit or none of it."""
        self._assert_mutable()
        checked_captures = []
        capture_metadata = []
        body_bytes = 0
        metadata_bytes = 0
        for event in itertools.islice(captures, 1001):
            if len(checked_captures) == 1000:
                raise ScanError("too many response observations in one page commit")
            if isinstance(event, type) or not is_dataclass(event):
                raise ScanError("native capture must be a typed transport observation")
            item = asdict(event)
            data = item.pop("entity_bytes")
            if data is not None and type(data) is not bytes:
                raise ScanError("native capture entity must be bytes or unavailable")
            body_bytes += len(data) if data is not None else 0
            item["entity_sha256"] = hashlib.sha256(data).hexdigest() if data is not None else None
            metadata_bytes += sum(map(len, _json_chunks(item)))
            if body_bytes > 8 * MAX_RECORD_BYTES or metadata_bytes > MAX_RECORD_BYTES:
                raise ScanError("native page response observations exceed the atomic input budget")
            capture_metadata.append(item)
            checked_captures.append(event)
        captures = checked_captures
        resources = _bounded_items(resources, "resource declarations", MAX_EDGES_PER_PAGE)
        candidates = _bounded_items(candidates, "ordered discovery candidates")
        partial_reasons = _bounded_items(partial_reasons, "partial reasons", 16)
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
        if capture_metadata:
            payload["captures"] = capture_metadata
        if resource_inventory_state is not None:
            payload["resources"] = {
                "declarations": resources,
                "state": resource_inventory_state,
                "omitted": resources_omitted,
            }
        if candidates or partial_reasons:
            payload.update(candidates=candidates, partial_reasons=partial_reasons)
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
            document_id = None
            if captures:
                from .corpus import store_response

                policy = json.loads(
                    self.con.execute("SELECT retention_json FROM scan").fetchone()[0]
                )
                if (
                    shutil.disk_usage(self.path.parent).free
                    < policy["min_free_bytes"] + body_bytes * 3 + MAX_RECORD_BYTES
                ):
                    raise ScanError("insufficient free disk for the next atomic response capture")
                for event in captures:
                    if event.requested_url != lease.url and event.requested_url not in {
                        hop.get("url") for hop in record.get("redirect_chain", [])
                    }:
                        raise ScanError(
                            "response does not belong to the page or its observed redirect diagnostics"
                        )
                    _response_id, observed_document = store_response(
                        self.con,
                        event,
                        purpose="page",
                        policy=policy,
                        logical_url=event.requested_url,
                    )
                    if event.requested_url == lease.url:
                        document_id = observed_document
                if document_id is None:
                    raise ScanError("page has no response for its frontier lease")
                self._record_session_change(captures)
                self._hit("after_bodies")
            page_row = self._page_row(record, lease)
            page_row["document_id"] = document_id
            _insert(self.con, "pages", page_row)
            self._hit("after_page")
            self._write_observations(lease, document_id, "static", links, forms)
            if resource_inventory_state is not None:
                from .resources import put_declarations

                put_declarations(
                    self.con,
                    page_url_id=lease.url_id,
                    document_id=document_id,
                    representation="static",
                    declarations=resources,
                    fetch_enabled=json.loads(
                        self.con.execute("SELECT config_json FROM scan").fetchone()[0]
                    )["resources"]["fetch"],
                    inventory_state=resource_inventory_state,
                    omitted=resources_omitted,
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
            from .frontier import apply_candidates

            apply_candidates(
                self.con,
                candidates,
                source=lease.url,
                queue_ordinal=lease.queue_ordinal,
                limit=query_limit,
            )
            self._partial_reasons(partial_reasons)
            self.con.execute("UPDATE frontier SET state='done' WHERE url_id=?", (lease.url_id,))
            self._hit("after_frontier")
            self._hit("before_runtime")
            self._write_runtime(runtime or {}, lease.depth)
            self._hit("after_runtime")
            for item in context:
                from .native_context import put_context

                put_context(self.con, item)
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
            self._sync_corpus()
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

    def _record_session_change(self, captures) -> None:
        if any(event.session_changed for event in captures):
            row = self.con.execute(
                "SELECT payload_json FROM context_items WHERE kind='credential_context' AND item_key='run'"
            ).fetchone()
            if row is not None:
                payload = json.loads(row[0])
                payload["implicit_state"] = True
                self.con.execute(
                    "UPDATE context_items SET payload_json=? WHERE kind='credential_context' AND item_key='run'",
                    (_dump(payload),),
                )

    def preflight_capture(self) -> None:
        """Check the configured free-space reserve before scheduling a request."""
        policy = json.loads(self.con.execute("SELECT retention_json FROM scan").fetchone()[0])
        if shutil.disk_usage(self.path.parent).free < policy["min_free_bytes"] + MAX_RECORD_BYTES:
            raise ScanError("insufficient free disk for native capture; no request was started")

    def commit_render(
        self,
        url: str,
        record: dict[str, Any] | None,
        *,
        html: str | bytes | None,
        renderer: dict[str, Any],
        captured_at: str,
        links: Iterable[dict[str, Any]] = (),
        forms: Iterable[dict[str, Any]] = (),
        representation: str = "rendered",
        body_state: str = "complete",
        body_reason: str = "none",
        captures: Iterable[Any] = (),
        partial_reasons: Iterable[str] = (),
        resources: Iterable[dict[str, Any]] = (),
        resource_inventory_state: str | None = None,
        resources_omitted: int = 0,
        elapsed_seconds: float | None = None,
    ) -> int:
        """Retain one render attempt and its accepted extraction atomically."""
        from .corpus import store_rendered_document, store_response

        self._assert_mutable()
        self.preflight_capture()
        if representation not in {"rendered", "legacy_fragment"}:
            raise ScanError("unsupported rendered representation")
        links = _bounded_items(links, "rendered links", MAX_EDGES_PER_PAGE)
        forms = _bounded_items(forms, "rendered forms", 2000)
        partial_reasons = _bounded_items(partial_reasons, "render partial reasons", 16)
        resources = _bounded_items(resources, "render resource declarations", MAX_EDGES_PER_PAGE)
        captures = list(itertools.islice(captures, 1001))
        if (
            len(captures) > 1000
            or sum(len(e.entity_bytes or b"") for e in captures) > 8 * MAX_RECORD_BYTES
        ):
            raise ScanError("render response observations exceed the atomic input budget")
        if html is not None and (
            type(html) not in {str, bytes}
            or len(html.encode("utf-8") if isinstance(html, str) else html) > 8 * MAX_RECORD_BYTES
        ):
            raise ScanError("rendered document exceeds the atomic input budget")
        for _ in _json_chunks(renderer):
            pass
        if record is not None:
            for _ in _json_chunks(record):
                pass
        self._begin()
        try:
            page = self.con.execute(
                "SELECT p.*,u.url,f.queue_ordinal FROM pages p JOIN urls u USING(url_id) JOIN frontier f USING(url_id) WHERE u.url=?",
                (url,),
            ).fetchone()
            if page is None:
                raise ScanError("rendered document has no committed static page")
            policy = json.loads(self.con.execute("SELECT retention_json FROM scan").fetchone()[0])
            if representation == "rendered":
                if captures:
                    raise ScanError("serialized DOM must not masquerade as an HTTP response")
                document_id = store_rendered_document(
                    self.con,
                    logical_url=url,
                    html=html,
                    renderer=renderer,
                    policy=policy,
                    captured_at=captured_at,
                    body_state=body_state,
                    body_reason=body_reason,
                )
            else:
                if not captures:
                    raise ScanError("legacy fragment requires a captured navigation response")
                document_id = None
                for event in captures:
                    _, document_id = store_response(
                        self.con,
                        event,
                        purpose="page",
                        policy=policy,
                        logical_url=url,
                        representation="legacy_fragment",
                        renderer=renderer,
                    )
                self._record_session_change(captures)
            self._hit("after_render_body")
            if record is not None:
                if record["representation"] != representation:
                    raise ScanError("rendered page representation differs from its document")
                lease = Lease(page["url_id"], url, page["crawl_depth"], page["queue_ordinal"])
                values = self._page_row(record, lease)
                values.pop("url_id")
                values["page_ordinal"] = page["page_ordinal"]
                values["document_id"] = document_id
                self.con.execute(
                    "UPDATE pages SET "
                    + ",".join(name + "=?" for name in values)
                    + " WHERE url_id=?",
                    (*values.values(), page["url_id"]),
                )
                self.con.execute(
                    "DELETE FROM links WHERE source_url_id=? AND evidence_representation=?",
                    (page["url_id"], representation),
                )
                self.con.execute(
                    "DELETE FROM forms WHERE page_url_id=? AND evidence_representation=?",
                    (page["url_id"], representation),
                )
                self._write_observations(lease, document_id, representation, links, forms)
                if resource_inventory_state is not None:
                    from .resources import put_declarations

                    put_declarations(
                        self.con,
                        page_url_id=lease.url_id,
                        document_id=document_id,
                        representation=representation,
                        declarations=resources,
                        fetch_enabled=json.loads(
                            self.con.execute("SELECT config_json FROM scan").fetchone()[0]
                        )["resources"]["fetch"],
                        inventory_state=resource_inventory_state,
                        omitted=resources_omitted,
                    )
            elif links or forms:
                raise ScanError("unaccepted render cannot replace graph observations")
            self._hit("after_render_page")
            if elapsed_seconds is not None:
                runtime = self.resume_snapshot()["runtime"]
                if elapsed_seconds < runtime["elapsed_seconds"]:
                    raise ScanError("render elapsed time cannot go backwards")
                runtime["elapsed_seconds"] = elapsed_seconds
                self._write_runtime(runtime, runtime["max_depth_reached"])
            self._partial_reasons(partial_reasons)
            self.con.execute(
                "UPDATE scan SET evidence_revision=evidence_revision+?",
                (len(captures) if representation == "legacy_fragment" else 1,),
            )
            self._sync_corpus()
            self._hit("before_render_commit")
            self.con.commit()
            return document_id
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

    def save_audit(self, document: dict[str, Any]) -> None:
        from . import MAX_JSON_BYTES, _audit, _sha
        from .native_audit import AuditSizeError, validate_audit

        self._assert_mutable()
        # Check the exact serialized shape before allocating another complete
        # audit string or opening the replacing transaction. Never truncate it.
        encoded_bytes = 0
        for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, indent=2).iterencode(
            document
        ):
            encoded_bytes += len(part.encode("utf-8"))
            if encoded_bytes > MAX_JSON_BYTES:
                raise AuditSizeError(
                    f"complete audit exceeds the saved JSON limit ({MAX_JSON_BYTES} bytes); "
                    "capture evidence is retained, but this audit cannot be saved"
                )
        raw = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        _audit(raw)
        self._begin()
        try:
            scan = dict(self.con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
            _insert(
                self.con,
                "audit",
                {
                    "singleton": 1,
                    "schema_version": document["schema_version"],
                    "analyzer_version": scan["writer_version"],
                    "analyzer_revision": scan["writer_revision"],
                    "evidence_revision": scan["evidence_revision"],
                    "document_json": raw,
                    "sha256": _sha(raw),
                    "created_at": _utc(),
                },
            )
            validate_audit(self.con, scan, required=True)
            self.con.commit()
        except BaseException:
            self._rollback()
            raise

    def finish_capture(
        self, reason: str = "finished", *, timeout_seconds: float = FINALIZATION_TIMEOUT_SECONDS
    ) -> bool:
        """Finalize the file independently of collection completeness and audit availability."""
        self._assert_mutable()
        if (
            type(reason) is not str
            or not reason
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ScanError("invalid capture finalization reason or deadline")
        ready = self._finalize_checkpoint(timeout_seconds)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            partial = self.con.execute(
                "SELECT crawl_partial FROM scan WHERE singleton=1"
            ).fetchone()[0]
            pending = self.con.execute(
                "SELECT 1 FROM frontier WHERE state IN ('queued','inflight') LIMIT 1"
            ).fetchone()
            lifecycle = "finished" if ready and not partial and not pending else "interrupted"
            self.con.execute(
                "UPDATE scan SET lifecycle=?,finish_reason=?,finished_at=? WHERE singleton=1",
                (
                    lifecycle,
                    reason if ready else "finalization_blocked",
                    _utc() if lifecycle == "finished" else None,
                ),
            )
            self.con.commit()
            return ready
        except BaseException:
            self._rollback()
            raise

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
                    "UPDATE scan SET lifecycle='finished', finish_reason=?, finished_at=? WHERE singleton=1",
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

    def _finish_reanalysis(self) -> bool:
        """Finalize a derived artifact without treating historical frontier work as resumable."""
        if (
            self.con.execute("SELECT source_kind FROM scan WHERE singleton=1").fetchone()[0]
            != "reanalysis"
        ):
            raise ScanError("only a derived artifact can use reanalysis finalization")
        if not self._finalize_checkpoint(FINALIZATION_TIMEOUT_SECONDS):
            raise ScanError("derived reanalysis finalization was blocked")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self.con.execute(
                "UPDATE scan SET lifecycle='finished',finish_reason='offline_reanalysis',finished_at=? WHERE singleton=1",
                (_utc(),),
            )
            self.con.commit()
            return True
        except BaseException:
            self._rollback()
            raise

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


def _reanalysis_capability(con: sqlite3.Connection) -> dict[str, str]:
    """Count admitted HTML inputs in SQL without reading body BLOBs or Python page lists."""
    config = json.loads(con.execute("SELECT config_json FROM scan WHERE singleton=1").fetchone()[0])
    parse_limit = config["limits"]["max_response_bytes"]
    required, usable = con.execute(
        """WITH active_static AS (
            SELECT d.url_id,MAX(d.document_id) AS document_id FROM documents d
            JOIN context_items c ON c.kind='resource_inventory' AND c.item_key='document:'||d.document_id
            WHERE d.representation='static' GROUP BY d.url_id
        )
        SELECT COUNT(*),COALESCE(SUM(CASE WHEN
            selected.body_state='complete' AND selected.body_sha256 IS NOT NULL
            AND raw.body_state='complete' AND raw.body_sha256 IS NOT NULL
            AND raw_body.decoded_bytes<=? AND selected_body.decoded_bytes<=?
            THEN 1 ELSE 0 END),0)
        FROM pages p
        LEFT JOIN documents selected ON selected.document_id=p.document_id
        LEFT JOIN active_static a ON a.url_id=p.url_id
        LEFT JOIN documents raw ON raw.document_id=CASE WHEN p.representation='static'
            THEN p.document_id ELSE a.document_id END
        LEFT JOIN bodies raw_body ON raw_body.sha256=raw.body_sha256
        LEFT JOIN bodies selected_body ON selected_body.sha256=selected.body_sha256
        WHERE lower(p.content_type) LIKE '%html%' AND p.status_code IS NOT NULL""",
        (parse_limit, parse_limit),
    ).fetchone()
    if usable and usable == required:
        return {"state": "complete", "reason": ""}
    if usable:
        return {
            "state": "partial",
            "reason": "some HTML documents lack retained raw or selected inputs",
        }
    return {"state": "unavailable", "reason": "no retained raw HTML inputs"}
