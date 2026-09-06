"""Discover and load Screaming Frog exports from a directory.

Works the same for mode A (the runner just wrote them) and mode B (the user
exported them by hand). Files are matched to logical export keys by filename
tokens, so any reasonable SF naming works, and reading is encoding-tolerant
(SF emits UTF-8-with-BOM or UTF-16, sometimes XLSX).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pandas as pd

# Logical export key -> filename token matchers.
#   "all": every token must appear in the filename (AND)
#   "none": no token may appear (used to keep tab exports from matching their
#           bulk ``*_inlinks`` cousins)
EXPORT_MATCHERS: dict[str, dict[str, list[str]]] = {
    # Master table — required. Both tokens matter: "internal" alone also
    # matches the partial per-type tabs (Internal:HTML, Internal:Images, ...),
    # which are missing every non-matching-type row and would falsely satisfy
    # the audit's one required source (#209).
    "internal_all": {"all": ["internal", "all"], "none": ["inlinks", "outlinks"]},
    # Response-code tabs (NOT the inlinks bulk exports).
    "resp_4xx": {"all": ["4xx"], "none": ["inlinks"]},
    "resp_5xx": {"all": ["5xx"], "none": ["inlinks"]},
    "resp_3xx": {"all": ["3xx"], "none": ["inlinks"]},
    "resp_no_response": {"all": ["no_response"], "none": ["inlinks"]},
    "resp_blocked": {"all": ["blocked"], "none": ["inlinks"]},
    # Inlinks bulk exports (the localization source).
    "inlinks_4xx": {"all": ["4xx", "inlinks"]},
    "inlinks_5xx": {"all": ["5xx", "inlinks"]},
    "inlinks_3xx": {"all": ["3xx", "inlinks"]},
    "all_inlinks": {"all": ["all_inlinks"]},
    # Sitemaps.
    # Every other sitemap tab's filename also ends in "urls_in_sitemap", and
    # sorted() reaches them first, so each is excluded by name.
    "sitemap_in": {"all": ["urls_in_sitemap"], "none": ["not", "non", "redirect", "orphan"]},
    "sitemap_not_in": {"all": ["urls_not_in_sitemap"]},
    "sitemap_orphan": {"all": ["orphan"]},
    "sitemap_non_indexable": {"all": ["non", "indexable", "sitemap"]},
    "sitemap_redirects": {"all": ["redirect", "sitemap"]},
    "sitemap_non_200": {"all": ["non", "200", "sitemap"]},
    # Images / structured data / titles / etc. (consumed when present).
    "images_missing_alt": {"all": ["missing_alt"]},
    "images_over_kb": {"all": ["images", "kb"]},
    "images_missing_size": {"all": ["missing_size"]},
    "titles_duplicate": {"all": ["page_titles", "duplicate"]},
    "titles_multiple": {"all": ["page_titles", "multiple"]},
    # Native hreflang error report (Directives:Hreflang) — list of flagged URLs.
    # Every URL routed here is reported as HREFLANG_ERROR, so the tabs that
    # list annotated pages rather than problems ("All", "Contains Hreflang")
    # are excluded, as is the link-level bulk export below.
    "hreflang": {
        "all": ["hreflang"],
        "none": ["all_hreflang", "hreflang_all", "contains_hreflang"],
    },
    # Bulk Export → Links → All Hreflang: one row per hreflang annotation
    # (Source → Destination + lang). Drives the hreflang-graph checks (§7).
    "all_hreflang": {"all": ["all_hreflang"]},
    "desc_duplicate": {"all": ["description", "duplicate"]},
    "redirect_chains": {"all": ["redirect_chains"]},
    # Crawl Overview is intentionally unregistered (#286): SF writes it as a
    # two-column metadata header followed by a five-column table in one CSV,
    # a shape no consumer here parses, and registering the key only turned a
    # correctly-written export into a false "read error" in every audit run.
    # Native filter exports (activate matching checks when present; else skipped).
    "security_mixed": {"all": ["mixed_content"]},
    "security_hsts": {"all": ["hsts"]},
    "structured_data_missing": {"all": ["structured", "missing"]},
}

# Unambiguous codecs only. ``latin-1`` used to close this tuple, but it is a
# single-byte codec with a mapping for every byte 0x00-0xFF, so it can never
# raise ``UnicodeDecodeError`` -- it isn't a fallback that can fail like the
# other three, it's a silent catch-all that "succeeds" on any input, including
# a cp1251 (Windows Cyrillic) export, remapping each byte to the wrong letter
# (#160). Anything these three reject goes to ``_sniff_encoding`` instead.
READ_ENCODINGS = ("utf-8-sig", "utf-16", "utf-8")


@dataclass
class LoadedExports:
    """All exports found in a directory, plus the bookkeeping reporters need."""

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # key -> path
    # key -> codec that actually decoded it, so a report can tell a reviewer
    # where to look when text reads as mojibake despite the run "succeeding".
    encodings: dict[str, str] = field(default_factory=dict)
    found: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def get(self, key: str) -> pd.DataFrame | None:
        return self.frames.get(key)

    def has(self, key: str) -> bool:
        return key in self.frames


def _sniff_encoding(path: str) -> str | None:
    """Guess a codec for bytes none of ``READ_ENCODINGS`` could decode.

    ``charset_normalizer`` scores candidates by script and byte-frequency
    consistency rather than merely checking "is this a legal byte sequence" --
    the check every single-byte codec, ``latin-1`` included, always passes.
    It returns ``None`` when nothing clears its own bar (verified against
    random high-byte data), so a genuinely undecodable file still falls
    through to the caller's ``raise`` instead of being silently accepted.
    """
    from charset_normalizer import from_path

    best = from_path(path).best()
    return best.encoding if best is not None else None


def read_table(path: str) -> pd.DataFrame:
    """Read a CSV/XLSX export, trying encodings SF is known to emit.

    The codec that actually decoded the file is recorded on the returned
    DataFrame's ``.attrs["encoding"]`` so ``load_exports`` can surface it;
    before #160, nothing recorded which of the fallback encodings matched, so
    a cp1251 export silently mojibake'd with no signal downstream.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path)
        df.attrs["encoding"] = "xlsx"
        return df
    last_err: Exception | None = None
    for enc in READ_ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=enc, low_memory=False)
        except (UnicodeDecodeError, UnicodeError) as err:
            last_err = err
            continue
        except pd.errors.ParserError as err:
            # Wrong encoding can masquerade as a parser error (UTF-16 read as UTF-8).
            last_err = err
            continue
        df.attrs["encoding"] = enc
        return df
    detected = _sniff_encoding(path)
    if detected is not None:
        try:
            df = pd.read_csv(path, encoding=detected, low_memory=False)
            df.attrs["encoding"] = detected
            return df
        except (UnicodeDecodeError, UnicodeError, LookupError) as err:
            last_err = err
        except pd.errors.ParserError as err:
            last_err = err
    raise ValueError(f"Could not decode export {path!r}: {last_err}")


def _matches(filename: str, matcher: dict[str, list[str]]) -> bool:
    # Word separators are normalised: the same export is saved as
    # "all_hreflang.csv" or "all-hreflang.csv" depending on who exported it.
    low = filename.lower().replace("-", "_").replace(" ", "_")
    for token in matcher.get("all", []):
        if token.lower() not in low:
            return False
    for token in matcher.get("none", []):
        if token.lower() in low:
            return False
    return bool(matcher.get("all"))


def discover_exports(exports_dir: str) -> dict[str, str]:
    """Map each logical export key to its matching file path in a dir.

    Raises when two files satisfy the same key: silently keeping whichever
    sorts first would let a stale export replace the evidence the caller
    asked for with nothing in the run metadata to say a choice was even made
    (#210). The caller decides which file to keep by removing the other.
    """
    if not os.path.isdir(exports_dir):
        raise NotADirectoryError(f"Exports directory not found: {exports_dir}")
    candidates = [
        name
        for name in sorted(os.listdir(exports_dir))
        if name.lower().endswith((".csv", ".xlsx", ".xls"))
    ]
    found: dict[str, str] = {}
    for key, matcher in EXPORT_MATCHERS.items():
        matches = [name for name in candidates if _matches(name, matcher)]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous export for {key!r}: {len(matches)} files match it in "
                f"{exports_dir!r}: {', '.join(matches)}. Keep only one."
            )
        if matches:
            found[key] = os.path.join(exports_dir, matches[0])
    return found


def load_exports(exports_dir: str, required: tuple[str, ...] = ("internal_all",)) -> LoadedExports:
    """Discover and read every recognized export; report what's missing."""
    paths = discover_exports(exports_dir)
    result = LoadedExports()
    read_errors: dict[str, tuple[str, Exception]] = {}
    for key, path in paths.items():
        try:
            frame = read_table(path)
            result.frames[key] = frame
            result.files[key] = path
            result.encodings[key] = frame.attrs.get("encoding", "")
            result.found.append(key)
        except Exception as err:
            result.missing.append(f"{key} (read error: {err})")
            read_errors[key] = (path, err)
    for key in EXPORT_MATCHERS:
        if key not in result.frames and key not in (m.split(" ")[0] for m in result.missing):
            result.missing.append(key)
    for key in required:
        if key not in result.frames:
            if key in read_errors:
                path, err = read_errors[key]
                raise FileNotFoundError(
                    f"Required export {key!r} was found at {path!r} but could not be read: {err}."
                )
            raise FileNotFoundError(
                f"Required export {key!r} not found in {exports_dir!r}. "
                "Export at least Internal:All from Screaming Frog."
            )
    return result
