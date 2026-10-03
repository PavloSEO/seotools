"""Versioned, ordered SQLite storage for audits too large for the scan.v1 JSON slot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, suppress
from importlib.resources import files
from pathlib import Path
from typing import Any

from seohead import filesystem

from . import ScanError, open_scan

APPLICATION_ID = (ord("A") << 24) | (ord("U") << 16) | (ord("D") << 8) | ord("V")
USER_VERSION = 2
FORMAT_VERSION = "audit.v2"
COLLECTION_MARKER = "$audit_v2_collection"
MAX_HEADER_BYTES = 64 * 1024 * 1024
MAX_ITEM_BYTES = 8 * 1024 * 1024

_SCHEMA = tuple(
    statement.strip()
    for statement in files(__package__)
    .joinpath("audit_v2.sql")
    .read_text(encoding="utf-8")
    .split(";")
    if statement.strip()
)
_EXPECTED_SCHEMA = tuple(
    ("table", name, statement)
    for name, statement in zip(("audit_meta", "collections", "items"), _SCHEMA, strict=True)
)
_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class AuditV2Error(ScanError):
    """An audit.v2 companion is invalid, inconsistent, or exceeds an explicit boundary."""


def audit_v2_path(scan_path: str | Path) -> Path:
    """Return the deterministic companion path for a saved scan."""
    path = Path(scan_path)
    return path.with_name(path.name + ".audit-v2.sqlite")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise AuditV2Error(f"audit.v2 contains a non-JSON value: {exc}") from exc


def _pointer_parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise AuditV2Error("audit.v2 collection keys must be absolute JSON pointers")
    parts = []
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise AuditV2Error("audit.v2 collection pointer has invalid escaping")
        parts.append(token.replace("~1", "/").replace("~0", "~"))
    return parts


def _at_parent(root: Any, pointer: str) -> tuple[Any, str]:
    parts = _pointer_parts(pointer)
    parent = root
    for part in parts[:-1]:
        try:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise AuditV2Error(f"audit.v2 collection path does not exist: {pointer}") from exc
    return parent, parts[-1]


def _set_pointer(root: Any, pointer: str, value: Any) -> None:
    parent, key = _at_parent(root, pointer)
    try:
        if isinstance(parent, list):
            parent[int(key)] = value
        else:
            parent[key] = value
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise AuditV2Error(f"audit.v2 collection path does not exist: {pointer}") from exc


def _get_pointer(root: Any, pointer: str) -> Any:
    parent, key = _at_parent(root, pointer)
    try:
        return parent[int(key)] if isinstance(parent, list) else parent[key]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise AuditV2Error(f"audit.v2 collection path does not exist: {pointer}") from exc


def _collection_markers(value: Any) -> set[str]:
    markers = set()
    if isinstance(value, dict):
        if set(value) in ({COLLECTION_MARKER}, {COLLECTION_MARKER, "count"}):
            pointer = value.get(COLLECTION_MARKER)
            if isinstance(pointer, str):
                markers.add(pointer)
        for child in value.values():
            markers.update(_collection_markers(child))
    elif isinstance(value, list):
        for child in value:
            markers.update(_collection_markers(child))
    return markers


def _contains_marker(value: Any) -> bool:
    return bool(_collection_markers(value))


def _bind_to_scan(scan_path: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {"scan_uuid", "evidence_revision", "analyzer_version", "analyzer_revision"}
    if not isinstance(binding, Mapping) or set(binding) != expected_keys:
        raise AuditV2Error(
            "audit.v2 binding must name scan UUID, evidence revision and analyzer identity"
        )
    try:
        with closing(open_scan(scan_path, require_audit=False)) as con:
            scan = dict(con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
    except (TypeError, sqlite3.Error, ScanError) as exc:
        raise AuditV2Error(f"cannot validate audit.v2 scan binding: {exc}") from exc
    actual = {
        "scan_uuid": scan["scan_uuid"],
        "evidence_revision": scan["evidence_revision"],
        "analyzer_version": scan["writer_version"],
        "analyzer_revision": scan["writer_revision"],
    }
    candidate = dict(binding)
    if _canonical(candidate) != _canonical(actual):
        raise AuditV2Error("audit.v2 binding disagrees with its native scan identity")
    return actual


def _digest_start(binding_json: str, header_json: str) -> hashlib._Hash:
    digest = hashlib.sha256()
    for value in (FORMAT_VERSION, binding_json, header_json):
        raw = value.encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest


def _digest_item(digest: Any, pointer: str, ordinal: int, value_json: str) -> None:
    for value in (pointer, str(ordinal), value_json):
        raw = value.encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)


def write_audit_v2(
    scan_path: str | Path,
    header: Mapping[str, Any],
    collections: Mapping[str, Iterable[Any]],
    binding: Mapping[str, Any],
) -> Path:
    """Atomically write a versioned companion from ordered JSON-pointer iterables.

    Each collection path must identify a list in ``header``. The list is replaced by a
    count-bearing marker in the stored header; rows remain in ordinal order and are never
    assembled into a second document in memory.
    """
    path = Path(scan_path).absolute()
    companion = audit_v2_path(path)
    if not path.is_file() or path.is_symlink():
        raise AuditV2Error("audit.v2 requires an existing regular native scan")
    validated_binding = _bind_to_scan(path, binding)
    if not isinstance(header, Mapping) or not isinstance(collections, Mapping) or not collections:
        raise AuditV2Error("audit.v2 needs an object header and at least one collection")
    stored_header = json.loads(_canonical(dict(header)))
    if _contains_marker(stored_header):
        raise AuditV2Error("audit.v2 header uses a reserved collection marker")
    normalized = {}
    for pointer, rows in collections.items():
        _pointer_parts(pointer)
        if pointer in normalized or not isinstance(rows, Iterable):
            raise AuditV2Error("audit.v2 collection paths must be unique and iterable")
        value = _get_pointer(stored_header, pointer)
        if not isinstance(value, list):
            raise AuditV2Error(f"audit.v2 collection path must point to an array: {pointer}")
        _set_pointer(stored_header, pointer, {COLLECTION_MARKER: pointer})
        normalized[pointer] = rows
    binding_json = _canonical(validated_binding)
    header_json = _canonical(stored_header)
    if len(header_json.encode("utf-8")) > MAX_HEADER_BYTES:
        raise AuditV2Error("audit.v2 header exceeds its explicit 64 MiB limit")

    try:
        filesystem.require_locking()
    except OSError as exc:
        raise AuditV2Error(str(exc)) from exc
    lock_path = companion.with_name(companion.name + ".writer.lock")
    lock_fd = -1
    try:
        lock_fd = filesystem.open_lock(lock_path)
        filesystem.lock_exclusive(lock_fd)
    except OSError as exc:
        if lock_fd >= 0:
            os.close(lock_fd)
        raise AuditV2Error(f"audit.v2 companion already has a writer: {exc}") from exc
    fd = -1
    temporary = None
    try:
        if companion.is_symlink():
            raise AuditV2Error("audit.v2 companion path must not be a symlink")
        fd, temporary = tempfile.mkstemp(
            prefix=f".{companion.name}.", suffix=".tmp", dir=companion.parent
        )
        os.close(fd)
        fd = -1
        con = sqlite3.connect(temporary)
        try:
            con.execute("PRAGMA trusted_schema=OFF")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute(f"PRAGMA application_id={APPLICATION_ID}")
            con.execute(f"PRAGMA user_version={USER_VERSION}")
            for statement in _SCHEMA:
                con.execute(statement)
            con.execute("BEGIN IMMEDIATE")
            counts: dict[str, int] = {}
            for pointer, rows in normalized.items():
                con.execute("INSERT INTO collections(pointer,item_count) VALUES (?,0)", (pointer,))
                count = 0
                for row in rows:
                    try:
                        raw = _ENCODER.encode(row)
                    except (TypeError, ValueError) as exc:
                        raise AuditV2Error(
                            f"audit.v2 row {pointer}[{count}] is not JSON: {exc}"
                        ) from exc
                    if len(raw.encode("utf-8")) > MAX_ITEM_BYTES:
                        raise AuditV2Error(f"audit.v2 row exceeds 8 MiB at {pointer}[{count}]")
                    con.execute(
                        "INSERT INTO items(pointer,ordinal,value_json) VALUES (?,?,?)",
                        (pointer, count, raw),
                    )
                    count += 1
                con.execute("UPDATE collections SET item_count=? WHERE pointer=?", (count, pointer))
                counts[pointer] = count
            for pointer, count in counts.items():
                _set_pointer(stored_header, pointer, {COLLECTION_MARKER: pointer, "count": count})
            header_json = _canonical(stored_header)
            if len(header_json.encode("utf-8")) > MAX_HEADER_BYTES:
                raise AuditV2Error("audit.v2 header exceeds its explicit 64 MiB limit")
            digest = _digest_start(binding_json, header_json)
            for pointer in sorted(normalized):
                for ordinal, raw in con.execute(
                    "SELECT ordinal,value_json FROM items WHERE pointer=? ORDER BY ordinal",
                    (pointer,),
                ):
                    _digest_item(digest, pointer, ordinal, raw)
            con.execute(
                "INSERT INTO audit_meta VALUES (1,?,?,?,?)",
                (FORMAT_VERSION, binding_json, header_json, digest.hexdigest()),
            )
            con.commit()
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise AuditV2Error("audit.v2 companion failed SQLite quick_check")
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, companion)
        temporary = None
        filesystem.fsync_directory(companion.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if temporary:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
        raise
    finally:
        filesystem.unlock(lock_fd)
        os.close(lock_fd)
    return companion


class AuditV2Reader:
    """Validated, re-iterable access to an audit.v2 companion."""

    def __init__(self, scan_path: str | Path, *, verify_binding: bool = True) -> None:
        self.scan_path = Path(scan_path).absolute()
        self.path = audit_v2_path(self.scan_path)
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise AuditV2Error(f"audit.v2 companion is unavailable: {exc}") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AuditV2Error("audit.v2 companion must be a regular unaliased file")
        try:
            self.con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
            self.con.row_factory = sqlite3.Row
            self.con.execute("PRAGMA trusted_schema=OFF")
            self.con.execute("PRAGMA query_only=ON")
            self.con.execute("PRAGMA cache_size=-8192")
            self.con.execute("PRAGMA temp_store=FILE")
            self._validate(verify_binding=verify_binding)
        except BaseException:
            if hasattr(self, "con"):
                self.con.close()
            raise

    def _validate(self, *, verify_binding: bool) -> None:
        if (
            self.con.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or self.con.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION
        ):
            raise AuditV2Error("unsupported audit.v2 companion identity or version")
        objects = tuple(
            tuple(row)
            for row in self.con.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
        if objects != _EXPECTED_SCHEMA:
            raise AuditV2Error("audit.v2 companion schema differs")
        if self.con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AuditV2Error("audit.v2 companion failed SQLite quick_check")
        if self.con.execute("PRAGMA foreign_key_check").fetchone():
            raise AuditV2Error("audit.v2 companion has inconsistent foreign keys")
        row = self.con.execute("SELECT * FROM audit_meta WHERE singleton=1").fetchone()
        if row is None or row["format_version"] != FORMAT_VERSION:
            raise AuditV2Error("audit.v2 companion has no matching format header")
        if self.con.execute("SELECT COUNT(*) FROM audit_meta").fetchone()[0] != 1:
            raise AuditV2Error("audit.v2 companion must contain exactly one metadata row")
        self.binding = json.loads(row["binding_json"])
        self.header = json.loads(row["header_json"])
        if not isinstance(self.binding, dict) or not isinstance(self.header, dict):
            raise AuditV2Error("audit.v2 metadata must be JSON objects")
        if len(row["header_json"].encode("utf-8")) > MAX_HEADER_BYTES:
            raise AuditV2Error("audit.v2 header exceeds its explicit 64 MiB limit")
        self.collections = {}
        for collection in self.con.execute(
            "SELECT pointer,item_count FROM collections ORDER BY pointer"
        ):
            pointer, count = collection["pointer"], collection["item_count"]
            if type(count) is not int or count < 0:
                raise AuditV2Error("audit.v2 has an invalid collection count")
            marker = _get_pointer(self.header, pointer)
            if marker != {COLLECTION_MARKER: pointer, "count": count}:
                raise AuditV2Error(f"audit.v2 header and collection index disagree: {pointer}")
            actual = self.con.execute(
                "SELECT COUNT(*) FROM items WHERE pointer=?", (pointer,)
            ).fetchone()[0]
            bounds = self.con.execute(
                "SELECT MIN(ordinal),MAX(ordinal) FROM items WHERE pointer=?", (pointer,)
            ).fetchone()
            if actual != count or (count and tuple(bounds) != (0, count - 1)):
                raise AuditV2Error(f"audit.v2 rows are incomplete or unordered: {pointer}")
            self.collections[pointer] = count
        if not self.collections:
            raise AuditV2Error("audit.v2 companion has no collections")
        markers = _collection_markers(self.header)
        if markers != set(self.collections):
            raise AuditV2Error("audit.v2 header and collection index have different paths")
        digest = _digest_start(row["binding_json"], row["header_json"])
        for item in self.con.execute(
            "SELECT pointer,ordinal,value_json FROM items ORDER BY pointer,ordinal"
        ):
            try:
                json.loads(item["value_json"])
            except json.JSONDecodeError as exc:
                raise AuditV2Error("audit.v2 row is invalid JSON") from exc
            if len(item["value_json"].encode("utf-8")) > MAX_ITEM_BYTES:
                raise AuditV2Error("audit.v2 row exceeds 8 MiB")
            _digest_item(digest, item["pointer"], item["ordinal"], item["value_json"])
        if digest.hexdigest() != row["sha256"]:
            raise AuditV2Error("audit.v2 content hash does not match")
        if verify_binding and _bind_to_scan(self.scan_path, self.binding) != self.binding:
            raise AuditV2Error("audit.v2 binding disagrees with the native scan")

    def count(self, pointer: str) -> int:
        try:
            return self.collections[pointer]
        except KeyError as exc:
            raise AuditV2Error(f"audit.v2 collection does not exist: {pointer}") from exc

    def iter_collection(self, pointer: str) -> Iterator[Any]:
        if pointer not in self.collections:
            raise AuditV2Error(f"audit.v2 collection does not exist: {pointer}")
        cursor = self.con.execute(
            "SELECT value_json FROM items WHERE pointer=? ORDER BY ordinal", (pointer,)
        )
        for row in cursor:
            yield json.loads(row[0])

    def document_chunks(self, *, max_bytes: int | None = None) -> Iterator[str]:
        """Yield a compact, complete JSON document; optionally enforce a byte ceiling."""
        emitted = 0

        def emit(chunk: str) -> Iterator[str]:
            nonlocal emitted
            emitted += len(chunk.encode("utf-8"))
            if max_bytes is not None and emitted > max_bytes:
                raise AuditV2Error(f"legacy JSON export exceeds its {max_bytes}-byte limit")
            yield chunk

        def walk(value: Any) -> Iterator[str]:
            if isinstance(value, dict) and set(value) == {COLLECTION_MARKER, "count"}:
                pointer = value[COLLECTION_MARKER]
                if pointer not in self.collections or value["count"] != self.collections[pointer]:
                    raise AuditV2Error("audit.v2 document marker does not match collection index")
                yield from emit("[")
                first = True
                for item in self.iter_collection(pointer):
                    if not first:
                        yield from emit(",")
                    yield from emit(_ENCODER.encode(item))
                    first = False
                yield from emit("]")
            elif isinstance(value, dict):
                yield from emit("{")
                for index, (key, child) in enumerate(value.items()):
                    if index:
                        yield from emit(",")
                    yield from emit(_ENCODER.encode(key))
                    yield from emit(":")
                    yield from walk(child)
                yield from emit("}")
            elif isinstance(value, list):
                yield from emit("[")
                for index, child in enumerate(value):
                    if index:
                        yield from emit(",")
                    yield from walk(child)
                yield from emit("]")
            else:
                yield from emit(_ENCODER.encode(value))

        yield from walk(self.header)

    def materialize_legacy(self, *, max_bytes: int = 64 * 1024 * 1024) -> dict[str, Any]:
        """Explicitly materialize the compatibility document under a byte ceiling."""
        chunks = []
        try:
            for chunk in self.document_chunks(max_bytes=max_bytes):
                chunks.append(chunk)
        except MemoryError as exc:
            raise AuditV2Error("audit.v2 legacy materialization exceeded available memory") from exc
        return json.loads("".join(chunks))

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> AuditV2Reader:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
