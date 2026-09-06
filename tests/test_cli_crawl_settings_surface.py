"""Reaching every crawler setting from the command line, and saying the rate out loud.

Thirty-nine settings live behind ``--config``, and the CLI deliberately shows only
a handful of flags -- each named flag is a line standing between a new setting and
the config file. ``--set`` is the answer to that tension rather than an exception
to it: one flag reaches all of them, and a setting added tomorrow is reachable
with no CLI change at all.

``--max-urls-per-second`` earns its own name for a different reason. A site owner
says "no more than seven a second"; expressing that as ``speed.min_delay_seconds``
makes the operator invert it, and a decimal point in the wrong place is somebody's
site under load.
"""

from __future__ import annotations

import pytest

from seohead import cli
from seohead.crawl import settings as crawl_config


def _kwargs(*flags):
    """Through the real parser, so the flags are exercised as an operator types them
    -- a hand-built Namespace would leave source-flag metadata unpopulated and send
    _build_kwargs to read stdin."""
    args = cli.build_parser().parse_args(["crawl-site", "--url", "https://example.com", *flags])
    return cli._build_kwargs("crawl-site", args)[1]


# ── --set reaches the whole surface ──────────────────────────────────────────


def test_set_reaches_a_setting_that_has_no_flag_of_its_own():
    kw = _kwargs("--set", "speed.concurrency=4")
    assert kw["overrides"] == {"speed.concurrency": 4}
    assert crawl_config.load(overrides=kw["overrides"])["speed"]["concurrency"] == 4


@pytest.mark.parametrize(
    "assignment,expected",
    [
        ("speed.concurrency=4", 4),
        ("speed.min_delay_seconds=0.25", 0.25),
        ("sitemaps.auto_discover=true", True),
        ("sitemaps.auto_discover=no", False),
        ("scope.include_patterns=/blog/,/docs/", ["/blog/", "/docs/"]),
        ("http.user_agent=Example/1.0", "Example/1.0"),
    ],
)
def test_a_value_gets_the_type_its_default_has(assignment, expected):
    """A string reaching an int setting would fail deep inside the crawl, or worse,
    compare as a string and quietly behave differently."""
    _, value = crawl_config.parse_setting_assignment(assignment)
    assert value == expected and type(value) is type(expected)


def test_an_empty_list_value_is_an_empty_list_not_one_empty_pattern():
    """An empty pattern matches everything, which would silently widen a crawl."""
    assert crawl_config.parse_setting_assignment("scope.include_patterns=")[1] == []


@pytest.mark.parametrize("raw", ["treu", "enable", "", "2"])
def test_set_refuses_an_unrecognized_boolean_instead_of_turning_it_off(raw):
    """A typo in a result-affecting setting cannot silently mean false."""
    with pytest.raises(crawl_config.ConfigError, match=r"sitemaps\.auto_discover"):
        crawl_config.parse_setting_assignment(f"sitemaps.auto_discover={raw}")


def test_set_parses_header_mapping_before_the_crawl_starts():
    """The one mapping-valued leaf must not become a string and fail in the spider."""
    kwargs = _kwargs("--set", 'http.headers={"X-Audit":"seohead"}')
    assert kwargs["overrides"] == {"http.headers": {"X-Audit": "seohead"}}
    assert crawl_config.load(overrides=kwargs["overrides"])["http"]["headers"] == {
        "X-Audit": "seohead"
    }


@pytest.mark.parametrize("raw", ["X-Audit:seohead", "[]", '{"X-Audit": 1}'])
def test_set_refuses_invalid_header_mapping_before_the_crawl_starts(raw):
    with pytest.raises(crawl_config.ConfigError, match=r"http\.headers"):
        crawl_config.parse_setting_assignment(f"http.headers={raw}")


def test_load_refuses_wrongly_typed_headers_before_the_crawl_starts():
    with pytest.raises(crawl_config.ConfigError, match=r"http\.headers"):
        crawl_config.load(overrides={"http.headers": "X-Audit:seohead"})


def test_a_misspelled_path_names_the_setting_the_operator_meant():
    with pytest.raises(crawl_config.ConfigError) as exc:
        crawl_config.parse_setting_assignment("concurrency=4")
    assert "speed.concurrency" in str(exc.value)


@pytest.mark.parametrize("bad", ["speed.concurrency", "=4", "speed.nope=1"])
def test_malformed_assignments_are_refused(bad):
    with pytest.raises(crawl_config.ConfigError):
        crawl_config.parse_setting_assignment(bad)


# ── the rate flag, and the trap it was written into ──────────────────────────


def test_a_requested_rate_becomes_the_delay_that_produces_it():
    kw = _kwargs("--max-urls-per-second", "7")
    resolved = crawl_config.load(overrides=kw["overrides"])
    assert crawl_config.effective_request_rate(resolved) == pytest.approx(7.0)


def test_a_named_flag_that_was_not_given_does_not_erase_the_rate():
    """The defect this file exists for. Applying every named argument over the
    generic ones looks right until an absent one arrives as None: it overwrites
    the rate the operator asked for, load() then skips the None, and the crawl
    silently runs at the default. A rate cap that becomes a no-op is worse than
    no flag at all, because it was believed."""
    kw = _kwargs("--max-urls-per-second", "7")
    resolved = crawl_config.load(overrides=kw["overrides"])
    assert crawl_config.effective_request_rate(resolved) == pytest.approx(7.0)


def test_a_named_flag_that_was_given_still_wins():
    kw = _kwargs("--max-urls-per-second", "7", "--min-delay", "1.0")
    overrides = dict(kw["overrides"])
    overrides["speed.min_delay_seconds"] = kw["min_delay"]
    assert crawl_config.effective_request_rate(crawl_config.load(overrides=overrides)) == 1.0


@pytest.mark.parametrize("rate", [0, -1])
def test_a_rate_of_zero_or_less_is_refused_rather_than_meaning_unbounded(rate):
    with pytest.raises(crawl_config.ConfigError):
        crawl_config.delay_for_request_rate(rate)


def test_the_printed_rate_is_the_one_the_run_will_use(capsys):
    """The number is printed so an operator can catch a dangerous combination
    before the crawl. Printing one rate and running another is worse than
    printing nothing."""
    kw = _kwargs("--max-urls-per-second", "7")
    cli._print_effective_rate(kw)
    assert "7.00 req/s" in capsys.readouterr().err


# ── the override contract itself ─────────────────────────────────────────────


def test_a_nested_mapping_override_is_refused_by_name():
    """It replaces the whole subtree and takes its siblings' defaults with it; the
    failure then surfaced from validate() as a bare KeyError about a key the
    caller never mentioned."""
    with pytest.raises(crawl_config.ConfigError) as exc:
        crawl_config.load(overrides={"limits": {"max_urls": 50}})
    assert "limits.max_urls" in str(exc.value)


def test_a_dotted_override_leaves_its_siblings_alone():
    limits = crawl_config.load(overrides={"limits.max_urls": 50})["limits"]
    assert limits["max_urls"] == 50
    assert set(limits) == set(crawl_config.DEFAULTS["limits"])
