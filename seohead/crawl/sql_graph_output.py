"""Finite compatibility output for existing native audit consumers."""

import itertools

from seohead.storage import ScanError


def composition(graph, *, max_pages: int):
    pages = list(itertools.islice(graph.iter_composition_rows(), max_pages + 1))
    if len(pages) > max_pages:
        raise ScanError("native composition output exceeds its compatibility limit")
    boilerplate = [page["url"] for page in pages if page["boilerplate_only"]]
    findings = []
    if boilerplate:
        shown = ", ".join(boilerplate[:5])
        more = f" and {len(boilerplate) - 5} more" if len(boilerplate) > 5 else ""
        findings.append(
            f"{len(boilerplate)} page(s) are linked only from navigation, "
            f"header, sidebar, or footer — never from body content: {shown}{more}"
        )
    return {
        **graph.composition_metadata().as_dict(),
        "population": "crawled_destinations",
        "pages": pages,
        "pages_boilerplate_only": boilerplate,
        "findings": findings,
    }
