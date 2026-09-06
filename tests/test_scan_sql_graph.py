"""Cursor projections over scan.v1 links preserve current graph semantics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from seohead.crawl import link_findings
from seohead.crawl.linkgraph import inlink_composition
from seohead.crawl.sql_graph import StoredGraph


def _graph(tmp_path):
    con = sqlite3.connect(tmp_path / "graph.sqlite")
    con.row_factory = sqlite3.Row
    ddl = (Path(__file__).parents[1] / "seohead" / "storage" / "scan_v1.sql").read_text()
    con.executescript(ddl)
    ids = {}

    def url(value):
        con.execute("INSERT OR IGNORE INTO urls(url) VALUES(?)", (value,))
        return con.execute("SELECT url_id FROM urls WHERE url=?", (value,)).fetchone()[0]

    def page(value, content_type="text/html"):
        url_id = url(value)
        ordinal = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        con.execute(
            "INSERT INTO pages("
            "url_id,page_ordinal,status_code,content_type,size_bytes,response_time,redirect_url,"
            "title,meta_description,h1,h1_2,h2,canonical,meta_robots,x_robots,og_title,"
            "og_description,og_image,word_count,text_ratio,content_frames,content_frames_same_origin,"
            "crawl_depth,content_encoding,charset,doctype,viewport,head_count,body_count,head_not_first,"
            "invalid_head_elements,outlinks,external_outlinks,jsonld_blocks_found,jsonld_blocks_parsed,"
            "error,error_kind,cache_status,body_unavailable,representation,redirect_chain_json,final_url) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                url_id,
                ordinal,
                200,
                content_type,
                0,
                None,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                0,
                None,
                0,
                0,
                0,
                "",
                "",
                "",
                "",
                0,
                0,
                0,
                "",
                0,
                0,
                0,
                0,
                "",
                "",
                "",
                "",
                "static",
                "[]",
                value,
            ),
        )
        ids[value] = url_id

    def link(source, destination, position="", *, nofollow=False, rel=(), target="", raw_href=""):
        con.execute(
            "INSERT INTO links(source_url_id,destination_url_id,evidence_representation,ordinal,anchor,"
            "nofollow,position,rel_json,target,raw_href) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                ids[source],
                url(destination),
                "static",
                con.execute(
                    "SELECT COUNT(*) FROM links WHERE source_url_id=?", (ids[source],)
                ).fetchone()[0],
                "anchor",
                int(nofollow),
                position,
                json.dumps(list(rel)),
                target,
                raw_href,
            ),
        )

    def form(page_url, method, action, has_password):
        con.execute(
            "INSERT INTO forms(page_url_id,ordinal,evidence_representation,method,action,has_password) "
            "VALUES(?,?,?,?,?,?)",
            (
                ids[page_url],
                con.execute(
                    "SELECT COUNT(*) FROM forms WHERE page_url_id=?", (ids[page_url],)
                ).fetchone()[0],
                "static",
                method,
                action,
                int(has_password),
            ),
        )

    return con, page, link, form


def test_composition_and_raw_inlink_counts_match_legacy_for_recorded_page_destinations(tmp_path):
    con, page, link, _form = _graph(tmp_path)
    source = "https://example.test/source"
    port_page = "https://example.test:443/port"
    plain_page = "https://example.test/plain"
    page(source)
    page(port_page, content_type="image/png")  # Population is pages, not HTML-only pages.
    page(plain_page)
    link(source, port_page, "nav")
    link(source, port_page, "nav")
    link(source, port_page, "content")
    link(source, plain_page, "")
    link(source, "https://outside.example/x", "nav")
    link(source, "https://example.test/unfetched", "nav")
    fragment_page = plain_page + "#fragment"
    link(source, fragment_page, "nav")
    con.commit()

    graph = StoredGraph(con)
    edges = list(graph.iter_links())
    eligible = [
        edge for edge in edges if edge.destination in {port_page, plain_page, fragment_page}
    ]
    legacy = inlink_composition(eligible)
    rows = list(graph.iter_composition_rows())
    metadata = graph.composition_metadata().as_dict()

    assert rows == legacy["pages"]
    assert metadata == {
        "ok": True,
        "measured": legacy["measured"],
        "edges_classified": legacy["edges_classified"],
        "edges_unclassified": legacy["edges_unclassified"],
        "edges_nonpage_destination": 2,
        "classified_fraction": legacy["classified_fraction"],
        "pages_with_inlinks": legacy["pages_with_inlinks"],
    }
    assert list(graph.iter_inlink_counts()) == [
        {"url": plain_page, "inlinks": 1, "unique_inlinks": 1},
        {"url": port_page, "inlinks": 3, "unique_inlinks": 1},
    ]
    assert next(row for row in rows if row["url"] == fragment_page)["inlinks_total"] == 1
    con.close()


def test_composition_temp_population_restores_query_only_without_main_writes(tmp_path):
    con, page, link, _form = _graph(tmp_path)
    source, destination = "https://example.test/source", "https://example.test/a"
    page(source)
    page(destination)
    link(source, destination, "nav")
    con.commit()
    before = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    con.execute("PRAGMA query_only=ON")
    with StoredGraph(con) as graph:
        assert next(graph.iter_composition_rows())["url"] == destination
    assert con.execute("PRAGMA query_only").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM links").fetchone()[0] == before
    con.close()


def test_streamed_link_and_form_predicates_match_existing_pure_functions(tmp_path):
    con, page, link, form = _graph(tmp_path)
    source = "https://example.test/source"
    http_page = "http://example.test/plain"
    port_page = "https://example.test:443/port"
    page(source)
    page(http_page)
    page(port_page)
    link(source, "http://localhost/debug", target="", raw_href="http://localhost/debug")
    link(source, "https://outside.example/new", target="_blank", raw_href="//outside.example/new")
    link(source, port_page, nofollow=False)
    link(source, port_page, nofollow=True)
    form(source, "post", "http://example.test/send", False)
    form(http_page, "post", "https://example.test/send", True)
    con.commit()

    graph = StoredGraph(con)
    edges = list(graph.iter_links())
    forms = list(graph.iter_forms())
    assert list(graph.iter_localhost_findings()) == link_findings.outlinks_to_localhost(edges)
    assert list(
        graph.iter_follow_and_nofollow("example.test")
    ) == link_findings.follow_and_nofollow_inlinks(edges, "example.test")
    assert list(graph.iter_unsafe_cross_origin()) == link_findings.unsafe_cross_origin_links(edges)
    assert list(graph.iter_protocol_relative()) == link_findings.protocol_relative_links(edges)
    assert list(graph.iter_insecure_forms()) == link_findings.form_url_insecure(forms)
    assert list(
        graph.iter_password_forms_on_http()
    ) == link_findings.forms_on_http_pages_with_password(forms)
    con.close()
