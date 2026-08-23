"""Unit tests for ``_validate_base_url`` (task 3.1, change: test-suite-foundation).

Covers every behavior the task spec lists for the base-URL validator:

- Accepts ``http://localhost:8888``, ``https://api.example.com``,
  ``http://127.0.0.1``, and ``http://host.docker.internal`` (local hosts may
  use plain HTTP).
- Rejects a URL missing a scheme (``localhost:8888``), a non-HTTP scheme
  (``ftp://...``), and a non-local host without HTTPS (``http://api.example.com``).
- Strips a trailing slash (``http://localhost:8888/`` -> ``http://localhost:8888``).

Only ``_validate_base_url`` is tested here. Tests for ``_redact``,
``_validate_memory_id``, ``_error``, ``_int_env``, and
``_with_default_filters`` live in tasks 3.2-3.6.
"""

from __future__ import annotations

import pytest

from mem0_mcp_server.server import _validate_base_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8888",
        "https://api.example.com",
        "http://127.0.0.1",
        "http://host.docker.internal",
    ],
    ids=[
        "localhost-with-port",
        "https-non-local",
        "ipv4-loopback",
        "docker-host-alias",
    ],
)
def test_validate_base_url_accepts_valid_urls(url: str) -> None:
    """Valid URLs are returned unchanged (no trailing slash to strip).

    Note: all inputs are lowercase. The Task 3.1 spec lists only lowercase URLs
    and does not require host-case preservation, so a mixed-case accept case
    (e.g. ``http://LocalHost:8888``) is intentionally out of scope here. The
    implementation does not normalize case; if that ever becomes a contract it
    should be added as a separate spec'd case.
    """
    assert _validate_base_url(url) == url


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("localhost:8888", "http:// or https://"),
        ("ftp://example.com", "http:// or https://"),
        ("http://api.example.com", "HTTPS for non-local hosts"),
    ],
    ids=[
        "missing-scheme",
        "non-http-scheme",
        "non-local-without-https",
    ],
)
def test_validate_base_url_rejects_invalid_urls(url: str, match: str) -> None:
    """Invalid URLs raise ``ValueError`` with a diagnostic message."""
    with pytest.raises(ValueError, match=match):
        _validate_base_url(url)


def test_validate_base_url_strips_trailing_slash() -> None:
    """A single trailing slash is stripped from an otherwise-valid URL.

    Covers both the local HTTP path and the non-local HTTPS path so the strip
    is verified symmetrically across accept-case shapes, not just local HTTP.
    """
    assert _validate_base_url("http://localhost:8888/") == "http://localhost:8888"
    assert _validate_base_url("https://api.example.com/") == "https://api.example.com"


def test_validate_base_url_strips_multiple_trailing_slashes() -> None:
    """``rstrip('/')`` removes every trailing slash, not just one."""
    assert _validate_base_url("http://localhost:8888//") == "http://localhost:8888"
