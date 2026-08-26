"""Response-header relay from upstream back to the caller.

httpx transparently decompresses a gzipped upstream response before we ever
see resp.content -- forwarding the original content-encoding/content-length
headers unchanged mislabels the (larger, plain) body we actually send as
still being the original (smaller, gzipped) one. Found live: Groq's responses
come through Cloudflare gzip-compressed; the real client's own decoder choked
on the mismatched headers with an opaque low-level connection error, even
though the gateway had already forwarded and received a real 200 OK."""

from __future__ import annotations

import httpx
from parapetai_gateway.server.app import _passthrough_headers


def test_content_encoding_and_length_are_stripped() -> None:
    upstream_headers = httpx.Headers(
        [
            ("content-type", "application/json"),
            ("content-encoding", "gzip"),
            ("content-length", "576"),
            ("x-request-id", "req_123"),
        ]
    )

    forwarded = _passthrough_headers(upstream_headers)

    assert "content-encoding" not in forwarded
    assert "content-length" not in forwarded
    assert forwarded["content-type"] == "application/json"
    assert forwarded["x-request-id"] == "req_123"


def test_hop_by_hop_headers_are_stripped() -> None:
    upstream_headers = httpx.Headers(
        [("content-type", "application/json"), ("connection", "keep-alive"), ("te", "trailers")]
    )

    forwarded = _passthrough_headers(upstream_headers)

    assert forwarded == {"content-type": "application/json"}
