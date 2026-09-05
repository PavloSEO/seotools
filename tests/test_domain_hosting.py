"""hosting must not require an IPv4 address (issue #239).

``profile_domain`` selected a hosting address only from ``A`` records, so an
IPv6-only domain reported an empty ``hosting`` object even though its ``AAAA``
record carried a perfectly usable routable address. IPv4 must not be a hidden
prerequisite for a documented hosting IP/ASN/owner result.
"""

from seohead.recon import domain


def _stub_rdap(_path):
    return {"supported": True, "data": {"events": [], "entities": [], "nameservers": []}}


def _make_doh(records: dict[str, list[str]]):
    def _doh(_name, record_type):
        return records.get(record_type, [])

    return _doh


def test_ipv6_only_domain_gets_hosting_from_aaaa(monkeypatch):
    monkeypatch.setattr(domain, "rdap", _stub_rdap)
    monkeypatch.setattr(
        domain,
        "doh",
        _make_doh({"A": [], "AAAA": ["2001:db8::7"]}),
    )
    result = domain.profile_domain("v6-only.example", with_tls=False)

    assert result["dns"]["a"] == []
    assert result["dns"]["aaaa"] == ["2001:db8::7"]
    assert result["hosting"].get("ip") == "2001:db8::7"
    assert result["hosting"].get("ip_source") == "aaaa"
    # No invented IPv4 fallback anywhere in the hosting record.
    assert "127.0.0.1" not in str(result["hosting"])


def test_ipv4_domain_still_prefers_the_a_record(monkeypatch):
    """Positive control: an ordinary IPv4 domain keeps behaving as before."""
    monkeypatch.setattr(domain, "rdap", _stub_rdap)
    monkeypatch.setattr(
        domain,
        "doh",
        _make_doh({"A": ["93.184.216.34"], "AAAA": ["2001:db8::7"]}),
    )
    result = domain.profile_domain("dual-stack.example", with_tls=False)

    assert result["hosting"].get("ip") == "93.184.216.34"
    assert result["hosting"].get("ip_source") == "a"


def test_no_a_or_aaaa_leaves_hosting_empty_not_invented(monkeypatch):
    """Negative control: nothing resolves, so hosting stays empty, not guessed."""
    monkeypatch.setattr(domain, "rdap", _stub_rdap)
    monkeypatch.setattr(domain, "doh", _make_doh({"A": [], "AAAA": []}))
    result = domain.profile_domain("unresolvable.example", with_tls=False)

    assert result["hosting"] == {}
    assert "domain does not resolve to an IP address" in result["flags"]


def test_cymru_query_name_is_family_specific():
    assert domain._cymru_query_name("1.2.3.4") == "4.3.2.1.origin.asn.cymru.com"
    assert domain._cymru_query_name("2001:db8::7").endswith(".origin6.asn.cymru.com")
    assert domain._cymru_query_name("not-an-ip") is None


def test_reverse_dns_name_is_family_specific():
    assert domain._reverse_dns_name("1.2.3.4") == "4.3.2.1.in-addr.arpa"
    assert domain._reverse_dns_name("2001:db8::7").endswith(".ip6.arpa")
    assert domain._reverse_dns_name("not-an-ip") is None
