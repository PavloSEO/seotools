"""Value coercion and case-insensitive column mapping for SF exports.

Screaming Frog column headers and the module set in an export drift between
versions and configuration profiles. Everything here is tolerant: a
missing column yields ``None``, never an exception.
"""

from __future__ import annotations

import math
import urllib.parse
from collections.abc import Iterable
from typing import Any

import pandas as pd

# Canonical field -> ordered list of possible SF headers (matched case-insensitively).
# First match wins. Confirmed against a real SF 19.4 ``Internal:All`` export.
INTERNAL_FIELD_MAP: dict[str, list[str]] = {
    "url": ["Address", "URL"],
    "content_type": ["Content Type"],
    "status_code": ["Status Code"],
    "status": ["Status"],
    "indexability": ["Indexability"],
    "indexability_status": ["Indexability Status"],
    "title": ["Title 1", "Title"],
    "title_length": ["Title 1 Length"],
    "title_px": ["Title 1 Pixel Width"],
    "meta_description": ["Meta Description 1", "Meta Description"],
    "desc_length": ["Meta Description 1 Length"],
    "desc_px": ["Meta Description 1 Pixel Width"],
    "meta_keywords": ["Meta Keywords 1", "Meta Keywords"],
    "h1": ["H1-1"],
    "h1_length": ["H1-1 Length"],
    "h1_2": ["H1-2"],
    "h2": ["H2-1"],
    "h2_2": ["H2-2"],
    "meta_robots": ["Meta Robots 1"],
    "x_robots": ["X-Robots-Tag 1"],
    "canonical": ["Canonical Link Element 1", "Canonical Link Element"],
    "canonical_2": ["Canonical Link Element 2"],
    "meta_refresh": ["Meta Refresh 1"],
    "amphtml": ["amphtml Link Element", "AMP HTML"],
    "rel_next": ['rel="next" 1', "rel=next 1"],
    "rel_prev": ['rel="prev" 1', "rel=prev 1"],
    "size_bytes": ["Size (bytes)", "Size (Bytes)", "Size"],
    "transferred_bytes": ["Transferred (bytes)", "Transferred (Bytes)"],
    "word_count": ["Word Count"],
    # Native-crawl only (#360): SF exports no iframe inventory, so these stay
    # absent there and the check that reads them skips instead of reporting clean.
    "content_frames": ["Content Frames"],
    "content_frames_same_origin": ["Content Frames Same-Origin"],
    "sentence_count": ["Sentence Count"],
    "avg_words_per_sentence": ["Average Words Per Sentence"],
    "flesch": ["Flesch Reading Ease Score"],
    "readability": ["Readability"],
    "http_version": ["HTTP Version"],
    "cookies": ["Cookies"],
    "text_ratio": ["Text Ratio"],
    "crawl_depth": ["Crawl Depth"],
    "folder_depth": ["Folder Depth"],
    "link_score": ["Link Score"],
    "inlinks": ["Inlinks"],
    "unique_inlinks": ["Unique Inlinks"],
    "outlinks": ["Outlinks"],
    "unique_outlinks": ["Unique Outlinks"],
    "external_outlinks": ["External Outlinks"],
    "closest_similarity": ["Closest Similarity Match"],
    "near_duplicates": ["No. Near Duplicates"],
    "hash": ["Hash", "Page Hash"],
    "response_time": ["Response Time"],
    "last_modified": ["Last Modified"],
    "redirect_url": ["Redirect URL", "Redirect URI"],
    "redirect_type": ["Redirect Type"],
    "hreflang": ["Hreflang"],
    "structured_data": ["Structured Data"],
    "validation_errors": ["Validation Errors"],
    "og_title": ["OG:Title"],
    "og_description": ["OG:Description"],
    "og_image": ["OG:Image", "OG:Image URL"],
    "og_url": ["OG:URL"],
    "spelling_errors": ["Spelling Errors"],
    "grammar_errors": ["Grammar Errors"],
    # Not a default Screaming Frog column: present only from a native seohead
    # crawl (seohead.crawl.evidence) or a Custom Extraction column configured
    # under one of these names. See seohead/sf/core/lighthouse.py for which
    # static Lighthouse audit each field feeds.
    "content_encoding": ["Content-Encoding", "Content Encoding"],
    "meta_charset": ["Meta Charset", "Charset"],
    "doctype": ["Doctype", "Doctype Declaration"],
    "viewport": ["Viewport", "Meta Viewport", "Mobile Viewport"],
    # Not an SF column -- seohead.crawl's own evidence.py projection adds it
    # (#18). An SF export simply never has this column, so it resolves to
    # None there, same as any other frame a list-mode run cannot fill.
    "representation": ["Representation"],
    # Not an SF column either -- projects PageRecord.body_unavailable (#243).
    # Non-empty ("oversized") means the HTML body was too large to parse, so
    # every other on-page field on this row is "never measured", not
    # "observed absent". See seohead/sf/core/rules.py's *_MISSING checks,
    # which read this to withhold a finding instead of fabricating one.
    "body_unavailable": ["Body Unavailable"],
    # Element-position evidence (issue #123): also not a default SF column —
    # "was this element inside <head> once the parser resolved the tree"
    # needs the parse tree itself, which only a native seohead crawl has.
    # See seohead/sf/core/rules.py check_element_position/check_document_skeleton.
    "title_outside_head": ["Title Outside Head"],
    "meta_description_outside_head": ["Meta Description Outside Head"],
    "canonical_outside_head": ["Canonical Outside Head"],
    "directives_outside_head": ["Directives Outside Head"],
    "hreflang_outside_head": ["Hreflang Outside Head"],
    "head_count": ["Head Count"],
    "body_count": ["Body Count"],
    "head_not_first": ["Head Not First"],
    "invalid_head_elements": ["Invalid Head Elements"],
}

# Canonical field -> headers for a ``*:Inlinks`` bulk export.
INLINKS_FIELD_MAP: dict[str, list[str]] = {
    "type": ["Type"],
    "source_url": ["Source", "From"],
    "destination_url": ["Destination", "To"],
    "anchor": ["Anchor Text", "Anchor"],
    "alt_text": ["Alt Text"],
    "status_code": ["Status Code"],
    "status": ["Status"],
    "follow": ["Follow"],
    "rel": ["Rel"],
    "target": ["Target"],
    "link_position": ["Link Position"],
    "link_path": ["Link Path"],
    "link_origin": ["Link Origin"],
}

# Canonical field -> headers for the Bulk Export → Links → ``All Hreflang``
# report. Each row is one hreflang annotation (source page → target URL + lang).
# Column names vary by SF version/profile, so multiple candidates are tried.
HREFLANG_FIELD_MAP: dict[str, list[str]] = {
    "source_url": ["Source", "From", "Address"],
    "destination_url": ["Destination", "To", "Hreflang URL", "Hreflang Target URL"],
    "hreflang": ["Hreflang", "Hreflang Language", "Language", "Lang"],
}


def normalize_value(value: Any) -> Any:
    """Empty strings / NaN -> ``None``; strip strings; pass numbers through."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else None
    return value


def to_int(value: Any) -> int | None:
    value = normalize_value(value)
    if value is None:
        return None
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(f):  # inf/-inf/nan would crash int() or poison JSON
        return None
    return int(f)


def to_float(value: Any) -> float | None:
    value = normalize_value(value)
    if value is None:
        return None
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None
    # non-finite values are not valid JSON numbers — drop them
    return f if math.isfinite(f) else None


def is_true(value: Any) -> bool:
    value = normalize_value(value)
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "yes", "1", "y"}


# Fields coerced to numbers. Everything else is a stripped string or None.
INT_FIELDS = frozenset(
    {
        "status_code",
        "title_length",
        "title_px",
        "desc_length",
        "desc_px",
        "h1_length",
        "size_bytes",
        "transferred_bytes",
        "word_count",
        "content_frames",
        "content_frames_same_origin",
        "sentence_count",
        "crawl_depth",
        "folder_depth",
        "inlinks",
        "unique_inlinks",
        "outlinks",
        "unique_outlinks",
        "external_outlinks",
        "near_duplicates",
        "validation_errors",
        "spelling_errors",
        "grammar_errors",
        "head_count",
        "body_count",
    }
)
FLOAT_FIELDS = frozenset(
    {
        "text_ratio",
        "link_score",
        "response_time",
        "closest_similarity",
        "flesch",
        "avg_words_per_sentence",
    }
)


def norm_url(url: str | None) -> str:
    """Normalize a URL for equality/index lookups (strip, drop trailing /, fold scheme/host).

    Scheme and host are case-insensitive per RFC 3986 and safe to fold; the path, query and
    fragment are not — a case-sensitive server can serve ``/News`` and ``/news`` as different
    resources, and lowercasing the whole URL silently merged them into one key, hiding a
    broken or unreciprocated hreflang target and collapsing distinct link-graph nodes (#202).
    The trailing-slash fold stays: it is a deliberate many-to-one tolerance for a canonical
    written without one (see AuditContext._build_pages) and is unrelated to letter case.
    """
    url = (url or "").strip()
    if not url:
        return ""
    scheme, netloc, path, query, fragment = urllib.parse.urlsplit(url)
    path = path.rstrip("/")
    return urllib.parse.urlunsplit((scheme.lower(), netloc.lower(), path, query, fragment))


def find_column(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    """Return the actual column name matching any candidate, case-insensitively."""
    lower_map = {str(col).lower(): col for col in df.columns}
    for candidate in candidates:
        hit = lower_map.get(candidate.lower())
        if hit is not None:
            return hit
    return None


def resolve_columns(
    columns: Iterable[str], field_map: dict[str, list[str]]
) -> dict[str, str | None]:
    """Map each canonical field to its actual source column (once per frame)."""
    lower = {str(c).lower(): c for c in columns}
    resolved: dict[str, str | None] = {}
    for field_name, candidates in field_map.items():
        resolved[field_name] = next(
            (lower[c.lower()] for c in candidates if c.lower() in lower), None
        )
    return resolved


def records_from_df(df: pd.DataFrame, field_map: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Vectorized projection of a frame onto canonical records.

    Resolves columns once, coerces numerics with ``pd.to_numeric`` in bulk and
    materializes plain Python dicts — ~orders of magnitude faster than calling
    :func:`row_to_record` under ``iterrows`` on large exports, and JSON-safe
    (no numpy scalars, no non-finite floats).
    """
    resolved = resolve_columns(df.columns, field_map)
    n = len(df)
    data: dict[str, Any] = {}
    for field_name, src in resolved.items():
        data[field_name] = df[src] if src is not None else pd.Series([None] * n, index=df.index)
    sub = pd.DataFrame(data, index=df.index)

    for field_name in INT_FIELDS | FLOAT_FIELDS:
        if field_name in sub.columns:
            sub[field_name] = pd.to_numeric(sub[field_name], errors="coerce")

    records: list[dict[str, Any]] = sub.to_dict("records")
    for rec in records:
        for field_name, value in rec.items():
            if value is None:
                continue
            if hasattr(value, "item"):  # numpy scalar -> Python scalar
                value = value.item()
                rec[field_name] = value
            if isinstance(value, float):
                if not math.isfinite(value):  # NaN/inf (incl. blanks) -> None
                    rec[field_name] = None
                elif field_name in INT_FIELDS:
                    rec[field_name] = int(value)
            elif isinstance(value, str):
                stripped = value.strip()
                rec[field_name] = stripped if stripped else None
    return records


def row_to_record(row: pd.Series, field_map: dict[str, list[str]]) -> dict[str, Any]:
    """Single-row projection (kept for ad-hoc use; bulk path is records_from_df)."""
    resolved = resolve_columns(row.index, field_map)
    record: dict[str, Any] = {}
    for field_name, col in resolved.items():
        raw = row[col] if col is not None else None
        if field_name in INT_FIELDS:
            record[field_name] = to_int(raw)
        elif field_name in FLOAT_FIELDS:
            record[field_name] = to_float(raw)
        else:
            record[field_name] = normalize_value(raw)
    return record
