"""Standards-boundary regressions for captured HTTP entity decoding."""

from __future__ import annotations

import gzip

from seohead.crawl.capture import bounded_entity


def test_concatenated_gzip_members_retain_the_complete_decoded_entity_at_exact_limit():
    payload = gzip.compress(b"first ") + gzip.compress(b"second")
    expected = gzip.decompress(payload)

    assert expected == b"first second"
    assert bounded_entity(payload, "gzip", len(expected)) == expected
