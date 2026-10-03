"""Portable, declarative finding views stored with a local project."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .runtime import read_document, write_document
from .workspace import _load

FORMAT = "seohead.project-finding-views.v1"
VERSION = 1
MAX_VIEWS = 100
MAX_FILTER_VALUES = 100
MAX_PAGE_SIZE = 1000
MAX_OFFSET = 1_000_000
_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_CHECK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FIELDS = (
    "severity",
    "check",
    "url",
    "text",
    "status_code",
    "occurrences_count",
    "fix_hint",
    "details",
    "locations",
    "segment",
)
_SORT_FIELDS = {"severity", "check", "url", "occurrences_count", "status_code", "segment"}
_DEFAULT_COLUMNS = ["severity", "check", "url", "text", "fix_hint"]
# Ascending is low-to-high; descending puts the highest-severity findings first.
_SEVERITY_ORDER = {"notice": 0, "warning": 1, "critical": 2}


def _text(value: Any, field: str, limit: int = 512) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be non-empty text of at most {limit} characters")
    return value


def _name(value: Any) -> str:
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ValueError("view name must be a lowercase identifier of at most 64 characters")
    return value


def _url(value: Any, field: str) -> str:
    if type(value) is not str or len(value) > 2048:
        raise ValueError(f"{field} must be a bounded absolute HTTP(S) URL")
    from seohead.recon.net import normalize_url

    normalized = normalize_url(value)
    if normalized != value or urlsplit(value).scheme not in {"http", "https"}:
        raise ValueError(f"{field} must be a normalized absolute HTTP(S) URL")
    return value


def _string_list(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_FILTER_VALUES:
        raise ValueError(f"{field} must be a list of at most {MAX_FILTER_VALUES} values")
    result = []
    for item in value:
        if type(item) is not str or not item.strip() or len(item) > 2048:
            raise ValueError(f"{field} values must be non-empty bounded strings")
        if pattern and not pattern.fullmatch(item):
            raise ValueError(f"{field} contains an invalid value")
        result.append(item)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} values must be unique")
    return result


def validate_definition(value: Any) -> dict[str, Any]:
    """Normalize a closed view definition; no SQL, regex, or executable expression is accepted."""
    if not isinstance(value, dict) or set(value) - {
        "name",
        "filters",
        "sort",
        "columns",
        "page_size",
    }:
        raise ValueError("view definition has unsupported fields")
    name = _name(value.get("name"))
    filters = value.get("filters", {})
    if not isinstance(filters, dict) or set(filters) - {"severity", "check", "url", "segment"}:
        raise ValueError("view filters support severity, check, url, and segment only")
    normalized_filters: dict[str, list[str]] = {}
    for field, values in filters.items():
        if field == "severity":
            parsed = _string_list(values, "severity")
            if any(item not in _SEVERITY_ORDER for item in parsed):
                raise ValueError("severity filters support critical, warning, and notice")
            normalized_filters[field] = sorted(parsed, key=_SEVERITY_ORDER.__getitem__)
        elif field == "check":
            normalized_filters[field] = sorted(_string_list(values, "check", _CHECK))
        elif field == "url":
            normalized_filters[field] = sorted(
                _url(item, "URL filter") for item in _string_list(values, "url")
            )
        else:
            normalized_filters[field] = sorted(_string_list(values, "segment"))
        if not normalized_filters[field]:
            raise ValueError(
                f"{field} filter cannot be empty; omit it to leave the field unfiltered"
            )

    sort = value.get("sort", {"field": "severity", "direction": "desc"})
    if not isinstance(sort, dict) or set(sort) != {"field", "direction"}:
        raise ValueError("sort requires field and direction")
    if type(sort["field"]) is not str or sort["field"] not in _SORT_FIELDS:
        raise ValueError(
            "sort field must be severity, check, url, segment, occurrences_count, or status_code"
        )
    if type(sort["direction"]) is not str or sort["direction"] not in {"asc", "desc"}:
        raise ValueError("sort direction must be asc or desc")

    columns = value.get("columns", _DEFAULT_COLUMNS)
    if not isinstance(columns, list) or not 1 <= len(columns) <= len(_FIELDS):
        raise ValueError("columns must select between 1 and 10 finding fields")
    if any(type(column) is not str or column not in _FIELDS for column in columns):
        raise ValueError("columns contain an unsupported finding field")
    if len(columns) != len(set(columns)):
        raise ValueError("columns must be unique")

    page_size = value.get("page_size", 100)
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be within 1..{MAX_PAGE_SIZE}")
    return {
        "name": name,
        "filters": normalized_filters,
        "sort": {"field": sort["field"], "direction": sort["direction"]},
        "columns": list(columns),
        "page_size": page_size,
    }


def _identity(project_uuid: str, name: str) -> str:
    digest = hashlib.sha256(f"{project_uuid}\0{name}".encode()).hexdigest()
    return "view-" + digest[:24]


def _validate_store(document: Any, project_uuid: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "format",
        "version",
        "project_uuid",
        "revision",
        "views",
    }:
        raise ValueError("finding-views.json has an unsupported shape")
    if (
        document["format"] != FORMAT
        or type(document["version"]) is not int
        or document["version"] != VERSION
    ):
        raise ValueError("finding views use an unsupported schema version")
    if document["project_uuid"] != project_uuid:
        raise ValueError("finding views belong to a different project")
    if type(document["revision"]) is not int or document["revision"] < 1:
        raise ValueError("finding view store revision is invalid")
    views = document["views"]
    if not isinstance(views, dict) or len(views) > MAX_VIEWS:
        raise ValueError("finding view catalogue is invalid or too large")
    for name, entry in views.items():
        if not isinstance(entry, dict) or set(entry) != {
            "id",
            "schema_version",
            "revision",
            "definition",
        }:
            raise ValueError("saved finding view has an unsupported shape")
        name = _name(name)
        if entry["id"] != _identity(project_uuid, name):
            raise ValueError("saved finding view identity is invalid")
        if type(entry["schema_version"]) is not int or entry["schema_version"] != 1:
            raise ValueError("saved finding view schema version is unsupported")
        if type(entry["revision"]) is not int or entry["revision"] < 1:
            raise ValueError("saved finding view revision is invalid")
        definition = validate_definition(entry["definition"])
        if definition["name"] != name:
            raise ValueError("saved finding view name does not match its key")
    return document


def _read(directory: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
    root, project = _load(directory)
    document = read_document(root, "finding-views.json")
    if document is not None:
        _validate_store(document, project["project_uuid"])
    return root, project, document


def list_views(directory: str | Path) -> dict[str, Any]:
    """List named project views without reading or changing audit evidence."""
    _root, project, document = _read(directory)
    views = document["views"] if document else {}
    return {
        "ok": True,
        "schema_version": VERSION,
        "project_uuid": project["project_uuid"],
        "config_revision": document["revision"] if document else 0,
        "views": [
            {
                "id": entry["id"],
                "name": name,
                "schema_version": entry["schema_version"],
                "revision": entry["revision"],
                "definition": copy.deepcopy(entry["definition"]),
            }
            for name, entry in sorted(views.items())
        ],
    }


def show_view(directory: str | Path, name: str) -> dict[str, Any]:
    """Read one named view and its stable identity/config revisions."""
    _root, project, document = _read(directory)
    view_name = _name(name)
    entry = (document or {}).get("views", {}).get(view_name)
    if entry is None:
        raise ValueError(f"finding view {view_name!r} does not exist")
    return {
        "ok": True,
        "project_uuid": project["project_uuid"],
        "config_revision": document["revision"],
        "view": {
            "id": entry["id"],
            "name": view_name,
            "schema_version": entry["schema_version"],
            "revision": entry["revision"],
            "definition": copy.deepcopy(entry["definition"]),
        },
    }


def save_view(
    directory: str | Path, view: dict[str, Any], *, expected_revision: int
) -> dict[str, Any]:
    """Create or update one named view using an expected config revision."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    definition = validate_definition(view)
    root, project, current = _read(directory)
    revision = current["revision"] if current else 0
    if expected_revision != revision:
        raise ValueError(f"finding view revision conflict: current revision is {revision}")
    views = copy.deepcopy(current["views"]) if current else {}
    previous = views.get(definition["name"])
    if previous is None and len(views) >= MAX_VIEWS:
        raise ValueError(f"a project can store at most {MAX_VIEWS} finding views")
    views[definition["name"]] = {
        "id": _identity(project["project_uuid"], definition["name"]),
        "schema_version": 1,
        "revision": previous["revision"] + 1 if previous else 1,
        "definition": definition,
    }
    document = {
        "format": FORMAT,
        "version": VERSION,
        "project_uuid": project["project_uuid"],
        "revision": revision + 1,
        "views": views,
    }
    write_document(root, "finding-views.json", document, expected_revision=revision)
    return show_view(directory, definition["name"])


def _field(finding: dict[str, Any], field: str) -> Any:
    if field == "url":
        return finding.get("target_url") or finding.get("url")
    if field == "text":
        return finding.get("message") or finding.get("text")
    return finding.get(field)


def _segment_definitions(document: dict[str, Any]) -> list[dict[str, Any]] | None:
    config = (document.get("run") or {}).get("crawl_config") or {}
    if not isinstance(config, dict):
        return None
    analysis = config.get("analysis.segments") or []
    if not isinstance(analysis, list):
        return None
    if analysis:
        return analysis
    scope = config.get("scope.segments") or []
    if not isinstance(scope, list) or len(scope) > MAX_FILTER_VALUES:
        return None
    definitions = []
    for item in scope:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None
        rules = []
        if item.get("prefix"):
            rules.append({"op": "prefix", "field": "path", "value": item["prefix"]})
        if item.get("host"):
            rules.append({"op": "eq", "field": "host", "value": item["host"]})
        if item.get("pattern"):
            rules.append({"op": "regex", "field": "url", "value": item["pattern"]})
        definitions.append({"name": item["name"], "rules": rules})
    return definitions or None


def _assign_segments(
    document: dict[str, Any], rows: list[dict[str, Any]]
) -> tuple[dict[str, str], set[str]] | None:
    definitions = _segment_definitions(document)
    if not definitions:
        return None
    from urllib.parse import urlsplit

    from seohead.sf.core.segments import UNSEGMENTED, assign_segments

    pages = document.get("pages") or []
    page_records = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        url = page.get("url")
        if not isinstance(url, str) or not url:
            continue
        parts = urlsplit(url)
        page_records.append(
            {**page, "url": url, "path": parts.path, "host": (parts.hostname or "").lower()}
        )
    assignment = assign_segments(page_records, definitions)
    primary = assignment["primary"]
    known = {*assignment["order"], "default"}
    result: dict[str, str] = {}
    for row in rows:
        target = _field(row, "url")
        if not isinstance(target, str) or not target:
            result[str(id(row))] = "default"
        elif target in primary:
            segment = primary[target]
            result[str(id(row))] = "default" if segment in (None, UNSEGMENTED) else segment
        elif not document.get("pages") and not (document.get("run") or {}).get("crawl_partial"):
            # A complete audit with no retained page rows cannot prove a segment assignment.
            result[str(id(row))] = ""
        else:
            isolated = assign_segments(
                [
                    {
                        "url": target,
                        "path": urlsplit(target).path,
                        "host": (urlsplit(target).hostname or "").lower(),
                    }
                ],
                definitions,
            )["primary"].get(target)
            result[str(id(row))] = "default" if isolated in (None, UNSEGMENTED) else isolated
    return result, known


def _sort_key(value: Any, field: str) -> tuple[bool, Any]:
    if value is None or value == "":
        return True, None
    if field == "severity":
        rank = _SEVERITY_ORDER.get(str(value).lower())
        return (rank is None), rank
    elif field in {"occurrences_count", "status_code"}:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return False, float(value)
        return True, None
    return False, str(value).casefold()


def _apply_view_details(
    directory: str | Path,
    name: str,
    document: dict[str, Any],
    *,
    offset: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a public view result and the selected raw rows for report renderers."""
    if not isinstance(document, dict):
        raise ValueError("audit must be a JSON object")
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"offset must be within 0..{MAX_OFFSET}")
    stored = show_view(directory, name)
    view = stored["view"]
    definition = view["definition"]
    if "schema" in document or "findings" in document:
        from seohead.audit.site import SCHEMA

        if document.get("schema") != SCHEMA or not isinstance(document.get("findings"), list):
            raise ValueError("site audit must use the supported schema and findings list")
        rows_key = "findings"
    elif "schema_version" in document or "issues" in document:
        if (
            document.get("schema_version") != "2.0"
            or not isinstance(document.get("run"), dict)
            or not isinstance(document.get("summary"), dict)
            or not isinstance(document.get("issues"), list)
            or not isinstance(document.get("pages"), list)
        ):
            raise ValueError(
                "SF audit must use schema_version 2.0 and the supported issue/page lists"
            )
        rows_key = "issues"
    else:
        raise ValueError("audit must use the supported site-audit or SF audit schema")
    if any(not isinstance(row, dict) for row in document[rows_key]):
        raise ValueError("audit finding rows must be JSON objects")
    rows = document[rows_key]
    filters = definition["filters"]
    segment_needed = (
        "segment" in filters
        or "segment" in definition["columns"]
        or definition["sort"]["field"] == "segment"
    )
    segment_data = _assign_segments(document, rows) if segment_needed else None
    if ("segment" in filters or definition["sort"]["field"] == "segment") and segment_data is None:
        operation = "filtering" if "segment" in filters else "sorting"
        raise ValueError(
            f"segment {operation} is unavailable: source audit has no segment definitions"
        )
    segment_for, known_segments = segment_data if segment_data is not None else ({}, set())
    if "segment" in filters and not set(filters["segment"]) <= known_segments:
        unknown = sorted(set(filters["segment"]) - known_segments)
        raise ValueError(f"view names segments absent from this audit: {unknown}")

    matching: list[tuple[dict[str, Any], int, str | None]] = []
    missing_filter_counts = {field: 0 for field in filters}
    for ordinal, row in enumerate(rows):
        segment = segment_for.get(str(id(row))) if segment_data is not None else None
        keep = True
        for field, selected in filters.items():
            actual = segment if field == "segment" else _field(row, field)
            if actual is None or actual == "":
                missing_filter_counts[field] += 1
                keep = False
            elif str(actual) not in selected:
                keep = False
        if keep:
            matching.append((row, ordinal, segment))

    sort = definition["sort"]
    field = sort["field"]
    present, missing = [], []
    for item in matching:
        value = item[2] if field == "segment" else _field(item[0], field)
        missing_value, _key = _sort_key(value, field)
        (missing if missing_value else present).append(item)
    present.sort(
        key=lambda item: _sort_key(
            item[2] if field == "segment" else _field(item[0], field), field
        )[1],
        reverse=sort["direction"] == "desc",
    )
    matching = [*present, *missing]
    source_rows = document[rows_key]
    source_identity = (document.get("run") or {}).get("scan_uuid")
    if not isinstance(source_identity, str) or not source_identity:
        digest = hashlib.sha256()
        for source_row in source_rows:
            digest.update(
                json.dumps(
                    source_row,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            )
            digest.update(b"\0")
        source_identity = digest.hexdigest()
    page_size = definition["page_size"]
    selected = matching[offset : offset + page_size]
    missing_projection_counts = {column: 0 for column in definition["columns"]}
    for row, _ordinal, segment in matching:
        for column in definition["columns"]:
            value = segment if column == "segment" else _field(row, column)
            if value is None or value == "":
                missing_projection_counts[column] += 1
    output_rows = []
    for row, ordinal, segment in selected:
        values = {
            column: (segment if column == "segment" else _field(row, column))
            for column in definition["columns"]
        }
        missing_fields = [
            column for column, value in values.items() if value is None or value == ""
        ]
        identity_payload = json.dumps(
            row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        finding_id = hashlib.sha256(
            f"{source_identity}\0{ordinal}\0{identity_payload}".encode()
        ).hexdigest()
        output_rows.append(
            {"finding_id": finding_id, "fields": values, "missing_fields": missing_fields}
        )

    total = len(matching)
    has_more = offset + len(selected) < total
    result = {
        "ok": True,
        "state": "partial"
        if any(missing_filter_counts.values()) or any(missing_projection_counts.values())
        else "measured",
        "view": {
            "id": view["id"],
            "name": view["name"],
            "schema_version": view["schema_version"],
            "revision": view["revision"],
            "config_revision": stored["config_revision"],
        },
        "source": {
            "identity": source_identity,
            "schema_version": document.get("schema_version") or document.get("schema"),
            "rows_key": rows_key,
            "finding_exclusion_policy": copy.deepcopy(
                (document.get("run") or {}).get("finding_exclusion_policy")
            ),
            "finding_exclusions": copy.deepcopy(
                (document.get("summary") or {}).get("finding_exclusions")
            ),
        },
        "counts": {
            "source": len(source_rows),
            "matched": total,
            "returned": len(selected),
            "filtered": len(source_rows) - total,
            "missing_filter_fields": {
                key: value for key, value in missing_filter_counts.items() if value
            },
            "missing_projection_fields": {
                key: value for key, value in missing_projection_counts.items() if value
            },
        },
        "pagination": {
            "offset": offset,
            "page_size": page_size,
            "has_more": has_more,
            "truncated": has_more,
            "next_offset": offset + len(selected) if has_more else None,
        },
        "columns": list(definition["columns"]),
        "sort": copy.deepcopy(definition["sort"]),
        "items": output_rows,
    }
    selected_rows = []
    for (row, _ordinal, segment), projected in zip(selected, output_rows, strict=True):
        copied = copy.deepcopy(row)
        if segment_data is not None:
            copied["__view_segment"] = segment
        copied["__view_finding_id"] = projected["finding_id"]
        copied["__view_missing_fields"] = projected["missing_fields"]
        selected_rows.append(copied)
    return result, selected_rows


def apply_view_to_audit(
    directory: str | Path,
    name: str,
    document: dict[str, Any],
    *,
    offset: int = 0,
) -> dict[str, Any]:
    """Filter, sort, project and page findings without changing the audit document."""
    return _apply_view_details(directory, name, document, offset=offset)[0]
