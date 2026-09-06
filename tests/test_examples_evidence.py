"""The example drift gate must detect renderer-only changes to evidence."""

import pytest

from seohead.sf.reporters import md
from tests import test_examples_gate as gate


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("https://example.com/old-page", "https://wrong.example/other-page"),
        ("/html/body/main/article/p[3]/a", "/html/body/main/article/p[4]/a"),
    ],
)
def test_renderer_only_url_or_xpath_drift_fails_example_gate(
    tmp_path, monkeypatch, original, replacement
):
    render = md._render_link_table

    def changed_table(write, group):
        render(lambda line: write(line.replace(original, replacement)), group)

    monkeypatch.setattr(md, "_render_link_table", changed_table)
    with pytest.raises(AssertionError, match=r"examples/audit\.md is stale"):
        gate.test_examples_match_a_fresh_run(tmp_path, monkeypatch)


def test_normalization_preserves_paths_and_timestamps_inside_evidence():
    evidence = (
        "| https://example.test/2026-09-06T10:04:05Z | "
        "`/html/body/main/a` | `/tmp/source/report.csv` |"
    )
    assert gate._normalize_text(evidence) == evidence
