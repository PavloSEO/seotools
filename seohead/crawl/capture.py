"""Bounded, redacted transport observations for native body capture."""

from __future__ import annotations

import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from seohead.storage.bodies import decode_entity

_ALLOW = frozenset(
    {
        "content-type",
        "content-encoding",
        "location",
        "etag",
        "last-modified",
        "cache-control",
        "vary",
        "x-robots-tag",
        "content-language",
        "content-length",
        "user-agent",
        "accept",
        "accept-language",
        "accept-encoding",
        "host",
        "date",
        "expires",
        "age",
        "pragma",
        "if-none-match",
        "if-modified-since",
        "range",
        "content-range",
        "link",
    }
)


def header_pairs(headers: Any) -> tuple[tuple[str, str], ...]:
    items = (
        headers.multi_items()
        if hasattr(headers, "multi_items")
        else headers.items()
        if hasattr(headers, "items")
        else headers
    )
    return tuple((str(name).lower(), str(value)) for name, value in items)


def redact_headers(headers: Any) -> tuple[tuple[str, str], ...]:
    return tuple((name, value) for name, value in header_pairs(headers) if name in _ALLOW)


@dataclass(frozen=True)
class CaptureEvent:
    method: str
    requested_url: str
    effective_url: str
    redirect_history: tuple[dict[str, Any], ...]
    requested_at: str
    received_at: str
    status_code: int | None
    request_headers: tuple[tuple[str, str], ...]
    credentials_used: bool
    response_headers: tuple[tuple[str, str], ...]
    content_type: str
    content_encoding: str
    entity_bytes: bytes | None
    body_fidelity: str
    body_state: str
    body_reason: str
    error: str
    error_kind: str
    effective_status_code: int | None = None
    effective_headers: tuple[tuple[str, str], ...] = ()
    response_time: float | None = None
    session_changed: bool = False


class EntityLimitError(ValueError):
    """The response entity is truncated or exceeds its declared capture limit."""


class EntityDecodeError(EntityLimitError):
    """The declared HTTP content coding cannot be decoded as an entity."""


def bounded_entity(raw: bytes, content_encoding: str, limit: int) -> bytes:
    """Return a capped content-decoded entity without allocating a compression bomb."""
    if type(raw) is not bytes or type(limit) is not int or limit < 0:
        raise ValueError("entity capture requires bytes and a nonnegative limit")
    return bounded_entity_chunks((raw,), content_encoding, limit)


def bounded_entity_chunks(chunks: Iterable[bytes], content_encoding: str, limit: int) -> bytes:
    """Decode raw HTTP chunks incrementally under a finite decoded-byte cap."""
    encoding = (content_encoding or "").strip().lower()
    if type(limit) is not int or limit < 0:
        raise ValueError("entity capture limit must be nonnegative")
    if encoding in ("", "identity"):
        out = bytearray()
        for chunk in chunks:
            if len(chunk) > limit - len(out):
                raise EntityLimitError("entity exceeds capture limit")
            out.extend(chunk)
        return bytes(out)
    wbits = (
        16 + zlib.MAX_WBITS
        if encoding == "gzip"
        else zlib.MAX_WBITS
        if encoding == "deflate"
        else None
    )
    if wbits is None:
        raise EntityDecodeError("unsupported content encoding")
    try:
        decoder = None
        out = bytearray()
        prefix = bytearray()
        encoded_bytes = 0
        for chunk in chunks:
            encoded_bytes += len(chunk)
            if encoded_bytes > limit + 64 * 1024:
                raise EntityLimitError("encoded response exceeds capture limit")
            if decoder is None:
                prefix.extend(chunk)
                if len(prefix) < 2:
                    continue
                if encoding == "deflate" and not (
                    prefix[0] & 15 == 8 and (prefix[0] * 256 + prefix[1]) % 31 == 0
                ):
                    wbits = -zlib.MAX_WBITS
                decoder = zlib.decompressobj(wbits)
                chunk = bytes(prefix)
                prefix.clear()
            out.extend(decoder.decompress(chunk, limit + 1 - len(out)))
            if len(out) > limit or decoder.unconsumed_tail:
                raise EntityLimitError("compressed entity exceeds capture limit")
        if decoder is None or not decoder.eof or decoder.unused_data:
            raise EntityLimitError("compressed entity is truncated")
        return bytes(out)
    except zlib.error as exc:
        raise EntityDecodeError("compressed entity is invalid") from exc


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CaptureEvent",
    "bounded_entity",
    "bounded_entity_chunks",
    "decode_entity",
    "now_utc",
    "redact_headers",
]
