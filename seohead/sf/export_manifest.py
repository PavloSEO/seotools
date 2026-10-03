"""Supported Screaming Frog export requests for audit profiles.

The full profile is a projection of this manifest: every request has an exact
CLI name and either a parsed export key, a raw-evidence id, or both. The local
SF help check is the capability source; an issue being present in the analyzer
registry does not make an unsupported CLI export real.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExportRequest:
    group: str
    name: str
    keys: tuple[str, ...] = ()
    raw_id: str | None = None
    file_tokens: tuple[str, ...] = ()
    derived_keys: tuple[str, ...] = ()
    derived_required_columns: tuple[str, ...] = ()


FULL_EXPORT_MANIFEST: tuple[ExportRequest, ...] = (
    ExportRequest("tabs", "Internal:All", ("internal_all",)),
    ExportRequest("tabs", "Response Codes:Client Error (4xx)", ("resp_4xx",)),
    ExportRequest("tabs", "Response Codes:Server Error (5xx)", ("resp_5xx",)),
    ExportRequest("tabs", "Response Codes:Redirection (3xx)", ("resp_3xx",)),
    ExportRequest(
        "tabs",
        "Sitemaps:URLs in Sitemap",
        ("sitemap_in",),
        derived_keys=("sitemap_redirects", "sitemap_non_200"),
        derived_required_columns=("Status Code",),
    ),
    ExportRequest("tabs", "Sitemaps:URLs not in Sitemap", ("sitemap_not_in",)),
    ExportRequest("tabs", "Sitemaps:Orphan URLs", ("sitemap_orphan",)),
    ExportRequest("tabs", "Sitemaps:Non-Indexable URLs in Sitemap", ("sitemap_non_indexable",)),
    ExportRequest("tabs", "Page Titles:Multiple", ("titles_multiple",)),
    ExportRequest(
        "tabs",
        "Structured Data:Validation Errors",
        raw_id="structured_data_validation_errors",
        file_tokens=("structured", "data", "validation", "errors"),
    ),
    ExportRequest(
        "tabs",
        "Structured Data:Validation Warnings",
        raw_id="structured_data_validation_warnings",
        file_tokens=("structured", "data", "validation", "warnings"),
    ),
    ExportRequest("tabs", "Security:Mixed Content", ("security_mixed",)),
    ExportRequest("tabs", "Images:Missing Alt Text", ("images_missing_alt",)),
    ExportRequest("tabs", "Images:Missing Size Attributes", ("images_missing_size",)),
    ExportRequest(
        "bulk",
        "Response Codes:Internal & External:Client Error (4xx) Inlinks",
        ("inlinks_4xx",),
    ),
    ExportRequest(
        "bulk",
        "Response Codes:Internal & External:Server Error (5xx) Inlinks",
        ("inlinks_5xx",),
    ),
    ExportRequest(
        "bulk",
        "Response Codes:Internal & External:Redirection (3xx) Inlinks",
        ("inlinks_3xx",),
    ),
    ExportRequest("reports", "Redirects:Redirect Chains", ("redirect_chains",)),
    # These requested tabs keep useful audit evidence but are not mapped to a
    # rule or verdict by the current analyzer.
    ExportRequest(
        "tabs", "JavaScript:All", raw_id="javascript_all", file_tokens=("javascript", "all")
    ),
    ExportRequest(
        "tabs", "Canonicals:All", raw_id="canonicals_all", file_tokens=("canonicals", "all")
    ),
    ExportRequest("tabs", "H1:All", raw_id="h1_all", file_tokens=("h1", "all")),
    # Issue #715's new full-profile datasets.
    ExportRequest("tabs", "Hreflang:Non-200 hreflang URLs", ("hreflang",)),
    ExportRequest("reports", "Hreflang:All hreflang URLs", ("all_hreflang",)),
    ExportRequest("tabs", "Page Titles:Duplicate", ("titles_duplicate",)),
    ExportRequest("tabs", "Meta Description:Duplicate", ("desc_duplicate",)),
    ExportRequest("tabs", "Images:Over X KB", ("images_over_kb",)),
    ExportRequest("tabs", "Security:Missing HSTS Header", ("security_hsts",)),
    ExportRequest("tabs", "Structured Data:Missing", ("structured_data_missing",)),
    ExportRequest("tabs", "Response Codes:No Response", ("resp_no_response",)),
    ExportRequest("tabs", "Response Codes:Blocked by Robots.txt", ("resp_blocked",)),
)

OPTIONAL_EXPORTS = {
    ("bulk", "Links:All Inlinks"): ExportRequest("bulk", "Links:All Inlinks", ("all_inlinks",)),
}

CLI_HELP_FLAGS = {"tabs": "export-tabs", "bulk": "bulk-export", "reports": "save-report"}

_KIND_TO_HELP = {
    "tabs": "export-tabs",
    "bulk": "bulk-export",
    "reports": "save-report",
}


def profile_exports(manifest: tuple[ExportRequest, ...] = FULL_EXPORT_MANIFEST) -> dict[str, Any]:
    """Build the CLI config from the manifest so the manifest owns full-profile requests."""
    exports: dict[str, Any] = {"tabs": [], "bulk": [], "reports": [], "fetch_all_inlinks": False}
    for request in manifest:
        exports[request.group].append(request.name)
    return exports


def requests_from_config(exports: dict[str, Any]) -> tuple[ExportRequest, ...]:
    """Resolve configured requests to known keys; retain custom CLI requests verbatim."""
    by_name = {
        (request.group, request.name): request
        for request in (*FULL_EXPORT_MANIFEST, *OPTIONAL_EXPORTS.values())
    }
    requests: list[ExportRequest] = []
    seen: set[tuple[str, str]] = set()
    for group in ("tabs", "bulk", "reports"):
        names = exports.get(group, [])
        if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
            raise ValueError(f"exports.{group} must be a list of non-empty strings")
        for name in names:
            identity = (group, name)
            if identity in seen:
                raise ValueError(f"duplicate Screaming Frog export request: {group}:{name}")
            seen.add(identity)
            request = by_name.get(identity)
            if request is None:
                tokens = tuple(dict.fromkeys(_filename_tokens(name)))
                request = ExportRequest(
                    group, name, raw_id=f"custom:{group}:{name}", file_tokens=tokens
                )
            requests.append(request)
    if exports.get("fetch_all_inlinks"):
        identity = ("bulk", "Links:All Inlinks")
        if identity not in seen:
            requests.append(by_name[identity])
    return tuple(requests)


def is_full_manifest(exports: dict[str, Any]) -> bool:
    """Return whether config still requests every full-profile manifest item."""
    requested = {(request.group, request.name) for request in requests_from_config(exports)}
    required = {(request.group, request.name) for request in FULL_EXPORT_MANIFEST}
    return required <= requested


def resolved_profile(profile: str, exports: dict[str, Any]) -> str:
    """Do not label a hand-pruned ``full`` request as a full run."""
    if profile == "lite":
        return "lite"
    if profile == "full" and is_full_manifest(exports):
        return "full"
    return "custom"


def help_names(help_text: str, group: str) -> frozenset[str]:
    """Parse only the names printed under SF's documented per-export help heading."""
    option = _KIND_TO_HELP[group]
    marker = f"The option '--{option}' supports the following arguments:"
    lines = help_text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == marker) + 1
    except StopIteration:
        raise ValueError(f"Screaming Frog help omitted the --{option} capability list") from None
    names: list[str] = []
    for line in lines[start:]:
        name = line.strip()
        if not name:
            if names:
                break
            continue
        # Ignore normal launcher log lines if the platform sends them through
        # the same pipe. Export names themselves do not start with a timestamp.
        if re.match(r"\d{4}-\d\d-\d\d", name):
            continue
        names.append(name)
    if not names:
        raise ValueError(f"Screaming Frog help returned no names for --{option}")
    return frozenset(names)


def unsupported_requests(
    requests: tuple[ExportRequest, ...], capabilities: dict[str, frozenset[str]]
) -> list[ExportRequest]:
    return [
        request
        for request in requests
        if request.name not in capabilities.get(request.group, frozenset())
    ]


def output_tokens(request: ExportRequest) -> tuple[str, ...]:
    return request.file_tokens or tuple(dict.fromkeys(_filename_tokens(request.name)))


def validate_export_files(
    exports_dir: str,
    requests: tuple[ExportRequest, ...],
    *,
    derive_sitemap_status: bool = False,
) -> dict[str, Any]:
    """Require every requested file in this fresh directory, parsing raw-only evidence too."""
    from .core.loader import discover_exports, read_table

    if not os.path.isdir(exports_dir):
        raise NotADirectoryError(f"Screaming Frog export directory not found: {exports_dir}")
    by_key = discover_exports(exports_dir)
    candidates = [
        name
        for name in sorted(os.listdir(exports_dir))
        if name.lower().endswith((".csv", ".xls", ".xlsx"))
    ]
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    derived: dict[str, str] = {}
    for request in requests:
        files: set[str] = set()
        missing_keys = [key for key in request.keys if key not in by_key]
        for key in request.keys:
            path = by_key.get(key)
            if path:
                files.add(os.path.basename(path))
        if missing_keys:
            failures.append(
                f"{request.group} {request.name!r}: missing loader key(s) {', '.join(missing_keys)}"
            )
        if request.raw_id:
            tokens = output_tokens(request)
            matches = [
                name
                for name in candidates
                if all(
                    token in name.lower().replace("-", "_").replace(" ", "_") for token in tokens
                )
            ]
            if len(matches) != 1:
                reason = "missing" if not matches else f"ambiguous: {', '.join(matches)}"
                failures.append(f"{request.group} {request.name!r} ({request.raw_id}): {reason}")
            else:
                files.add(matches[0])
                try:
                    read_table(os.path.join(exports_dir, matches[0]))
                except Exception as exc:
                    failures.append(
                        f"{request.group} {request.name!r} ({request.raw_id}): unreadable ({exc})"
                    )
        derived_keys = request.derived_keys if derive_sitemap_status else ()
        for key in derived_keys:
            derived[key] = request.keys[0] if request.keys else ""
        rows.append(
            {
                "kind": request.group,
                "cli_name": request.name,
                "logical_keys": list(request.keys),
                "derived_keys": list(derived_keys),
                "derived_required_columns": (
                    list(request.derived_required_columns) if derived_keys else []
                ),
                "raw_id": request.raw_id,
                "files": sorted(files),
            }
        )
    if failures:
        raise RuntimeError(
            "Screaming Frog export manifest is incomplete; audit analysis was not started: "
            + "; ".join(failures)
        )
    return {"requests": rows, "derived_keys": derived}


def validate_loaded_requests(
    exports: Any,
    requests: tuple[ExportRequest, ...],
    *,
    derive_sitemap_status: bool = False,
) -> None:
    """Fail before rule execution when a requested table could not be parsed/derived."""
    missing: list[str] = []
    for request in requests:
        derived_keys = request.derived_keys if derive_sitemap_status else ()
        for key in (*request.keys, *derived_keys):
            if key in exports.frames:
                continue
            reason = exports.derivation_errors.get(key)
            if reason is None:
                reason = next(
                    (item for item in exports.missing if item == key or item.startswith(f"{key} ")),
                    "file was present but the expected logical table was not loaded",
                )
            missing.append(f"{request.group} {request.name!r} -> {key}: {reason}")
    if missing:
        raise RuntimeError(
            "Screaming Frog export manifest is incomplete; audit analysis was not started: "
            + "; ".join(missing)
        )


def _filename_tokens(name: str) -> list[str]:
    """Use SF's name parts as a conservative fallback for unconsumed custom exports."""
    return [token.lower() for token in re.findall(r"[A-Za-z0-9]+", name) if token.lower() != "x"]


def help_fingerprint(capabilities: dict[str, frozenset[str]]) -> str:
    """Stable capability identity that omits timestamps, paths, and launcher logs."""
    lines = [
        f"{group}:{name}" for group, names in sorted(capabilities.items()) for name in sorted(names)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
