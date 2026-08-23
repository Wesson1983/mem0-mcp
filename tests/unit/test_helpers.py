"""Unit tests for pure helpers (change: test-suite-foundation).

Covers ``_validate_base_url`` (task 3.1) and ``_redact`` (task 3.2):

``_validate_base_url``:
- Accepts ``http://localhost:8888``, ``https://api.example.com``,
  ``http://127.0.0.1``, and ``http://host.docker.internal`` (local hosts may
  use plain HTTP).
- Rejects a URL missing a scheme (``localhost:8888``), a non-HTTP scheme
  (``ftp://...``), and a non-local host without HTTPS (``http://api.example.com``).
- Strips a trailing slash (``http://localhost:8888/`` -> ``http://localhost:8888``).

``_redact``:
- Redacts ``api_key``, ``token``, ``bearer``, and ``authorization`` patterns.
- Truncates to the configured limit (default 500).
- Leaves non-sensitive text unchanged.

``_validate_memory_id`` (task 3.3):
- Accepts alphanumeric characters plus ``_`` and ``-``.
- Rejects the empty string, slashes, spaces, and special characters.

``_error`` (task 3.4):
- Returns ``{"error": code, "detail": detail}`` with no ``status`` key when
  ``status`` is omitted or ``None``.
- Returns ``{"error": code, "detail": detail, "status": status}`` when
  ``status`` is provided.
- Is a pure constructor (no mutation, no shared state).

Tests for ``_int_env`` and ``_with_default_filters`` live in tasks 3.5-3.6.
"""

from __future__ import annotations

import pytest

from mem0_mcp_server.server import _error, _redact, _validate_base_url, _validate_memory_id


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


# ---------------------------------------------------------------------------
# Tests for ``_redact`` (task 3.2)
# ---------------------------------------------------------------------------

# A 32-character value that satisfies the ``{20,}`` quantifier in every
# ``_REDACT_PATTERNS`` regex.  Only ``[A-Za-z0-9]`` characters are used so the
# value matches all four patterns uniformly.
_SECRET = "abcdef0123456789abcdef0123456789"

# A 62-character JWT-shaped value containing ``.``, ``_``, and ``=`` — the
# extra character classes (``[A-Za-z0-9_\-\.=]``) only the bearer and
# authorization regexes accept.  The ``.`` breaks the api_key/token value
# class (``[A-Za-z0-9_\-]``), so this value only matches patterns 3 and 4.
_JWT_VALUE = "eyJhbGci.eyJzdWIi.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c="


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- api_key patterns ---
        (f"api_key={_SECRET}", "api_key=[REDACTED]"),
        (f"api-key={_SECRET}", "api-key=[REDACTED]"),
        (f"apikey={_SECRET}", "apikey=[REDACTED]"),
        (f"API_KEY={_SECRET}", "API_KEY=[REDACTED]"),
        # --- token patterns ---
        (f"token={_SECRET}", "token=[REDACTED]"),
        (f"token: {_SECRET}", "token: [REDACTED]"),
        # --- bearer tokens ---
        (f"Bearer {_SECRET}", "Bearer [REDACTED]"),
        (f"bearer {_SECRET}", "bearer [REDACTED]"),
        # --- authorization headers ---
        (f"Authorization: {_SECRET}", "Authorization: [REDACTED]"),
        (f"authorization={_SECRET}", "authorization=[REDACTED]"),
        # --- bearer/authorization with ``.``/``=`` in the value (JWT-shaped) ---
        (f"Bearer {_JWT_VALUE}", "Bearer [REDACTED]"),
        (f"Authorization: {_JWT_VALUE}", "Authorization: [REDACTED]"),
    ],
    ids=[
        "api_key-equals",
        "api-key-hyphen-equals",
        "apikey-no-separator",
        "api_key-uppercase",
        "token-equals",
        "token-colon-space",
        "bearer-title-case",
        "bearer-lowercase",
        "authorization-colon",
        "authorization-equals",
        "bearer-jwt-dots-equals",
        "authorization-jwt-dots-equals",
    ],
)
def test_redact_redacts_sensitive_patterns(text: str, expected: str) -> None:
    """Each sensitive pattern (api_key, token, bearer, authorization) is
    replaced with ``[REDACTED]`` while the captured prefix (group 1) is
    preserved.

    The value portion must be 20+ characters to match the ``_REDACT_PATTERNS``
    regexes; ``_SECRET`` is 32 chars of alphanumerics and satisfies every
    pattern.  The ``bearer-jwt-dots-equals`` and ``authorization-jwt-dots-
    equals`` rows use ``_JWT_VALUE`` (containing ``.``, ``=``, ``_``) to
    exercise the wider ``[A-Za-z0-9_\\-.=]`` class only the bearer and
    authorization regexes accept.
    """
    assert _redact(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The opening quote after the separator is consumed by the second
        # ``["']?`` (the one outside the capture group, after ``[:=]``); the
        # closing quote is not part of the match and survives.
        (f'api_key="{_SECRET}"', 'api_key=[REDACTED]"'),
        (f'Token: "{_SECRET}"', 'Token: [REDACTED]"'),
    ],
    ids=[
        "api_key-quoted-value",
        "token-colon-quoted-value",
    ],
)
def test_redact_quoted_values_consume_opening_quote(
    text: str, expected: str
) -> None:
    """When the value is wrapped in quotes the opening quote after the
    separator is consumed by the regex's second ``["']?`` (the one outside
    the capture group, after ``[:=]``), and the closing quote remains after
    ``[REDACTED]``.

    This documents the regex's actual behavior rather than an idealized
    "quote-pair-aware" redaction — the regex is deliberately simple.
    """
    assert _redact(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("api_key=short123", "api_key=short123"),
        ("token=short", "token=short"),
        ("Bearer shorttoken", "Bearer shorttoken"),
        ("Authorization: short", "Authorization: short"),
    ],
    ids=[
        "api_key-short-value",
        "token-short-value",
        "bearer-short-value",
        "authorization-short-value",
    ],
)
def test_redact_preserves_values_below_20_char_threshold(
    text: str, expected: str
) -> None:
    """Values shorter than 20 characters do not match ``{20,}`` and are left
    intact — the regex threshold prevents false positives on short tokens.
    """
    assert _redact(text) == expected


def test_redact_truncates_to_custom_limit() -> None:
    """A non-sensitive string longer than ``limit`` is truncated to exactly
    ``limit`` characters."""
    text = "a" * 100
    assert _redact(text, limit=10) == "aaaaaaaaaa"


def test_redact_truncates_to_default_limit() -> None:
    """The default limit is 500 characters; a longer string is truncated."""
    text = "x" * 600
    result = _redact(text)
    assert result == "x" * 500


def test_redact_does_not_truncate_string_at_or_below_limit() -> None:
    """A string at exactly the limit is returned in full (no truncation)."""
    text = "y" * 500
    assert _redact(text) == "y" * 500


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        # 480 chars of padding + "api_key=" (8) + 40-char secret = 528 chars.
        # Redact-first -> 480 + "api_key=[REDACTED]" (18) = 498 <= 500, so the
        # full marker survives.  Truncate-first -> [:500] leaves 12 secret
        # chars (< 20), the regex misses, and "api_key=aaaaaaaaaaaa" leaks.
        ("x" * 480 + f"api_key={'a' * 40}", 500, "x" * 480 + "api_key=[REDACTED]"),
        # 10 chars of padding + "api_key=" (8) + 40-char secret = 58 chars.
        # Redact-first -> 10 + "api_key=[REDACTED]" (18) = 28, then [:20]
        # truncates the marker to "api_key=[R".  Truncate-first -> [:20]
        # leaves 2 secret chars (< 20), the regex misses, and "api_key=aa"
        # leaks.
        ("x" * 10 + f"api_key={'a' * 40}", 20, "x" * 10 + "api_key=[R"),
    ],
    ids=["secret-straddles-boundary", "marker-truncated-not-secret"],
)
def test_redact_redacts_before_truncating_at_boundary(
    text: str, limit: int, expected: str
) -> None:
    """Redaction runs before truncation so no secret fragment leaks at the
    truncation boundary.

    A truncate-then-redact order reversal would cut a secret below the 20-char
    regex threshold, skip redaction, and leak raw secret characters — both
    rows fail under that mutant (the expected ``[REDACTED]`` marker, whole or
    truncated, is absent from the mutant output).
    """
    result = _redact(text, limit=limit)
    # The exact-output assert catches the order reversal: under truncate-first
    # the secret is cut below the 20-char threshold, the regex misses, and raw
    # secret characters appear where ``[REDACTED]`` (whole or truncated) should
    # be.  The negative guard below states the security intent explicitly: no
    # unredacted secret value follows a sensitive prefix in the output.
    assert result == expected
    assert "api_key=a" not in result


@pytest.mark.parametrize(
    "text",
    [
        "Hello, world! This is a test.",
        "The quick brown fox jumps over the lazy dog.",
        "No secrets here, just ordinary text content.",
    ],
    ids=[
        "greeting",
        "pangram",
        "ordinary-text",
    ],
)
def test_redact_leaves_non_sensitive_text_unchanged(text: str) -> None:
    """Text with no matching sensitive patterns is returned as-is (modulo
    truncation, which does not apply when the text is under the 500-char
    default limit)."""
    assert _redact(text) == text


def test_redact_redacts_multiple_secrets_in_one_string() -> None:
    """All four sensitive patterns present in a single string are redacted in
    one pass — the function iterates over every ``_REDACT_PATTERNS`` regex."""
    text = (
        f"api_key={_SECRET} and token={_SECRET} and Bearer {_SECRET} "
        f"and Authorization: {_SECRET}"
    )
    expected = (
        "api_key=[REDACTED] and token=[REDACTED] and Bearer [REDACTED] "
        "and Authorization: [REDACTED]"
    )
    assert _redact(text) == expected


def test_redact_redacts_within_longer_text() -> None:
    """Sensitive values embedded in surrounding non-sensitive text are redacted
    while the surrounding text is preserved."""
    text = f"Config: api_key={_SECRET}; done."
    expected = "Config: api_key=[REDACTED]; done."
    assert _redact(text) == expected


# ---------------------------------------------------------------------------
# Tests for ``_validate_memory_id`` (task 3.3)
# ---------------------------------------------------------------------------
#
# The regex is ``^[A-Za-z0-9_\-]+$``.  The empty string is rejected by the
# ``not memory_id`` guard in ``_validate_memory_id`` (which short-circuits
# before the regex is ever consulted), not by the ``+`` quantifier.  The
# ``+`` quantifier is a defense-in-depth backstop that the public function
# cannot isolate (the guard masks it); the single-char accept row below pins
# its minimum-length boundary (exactly 1).


@pytest.mark.parametrize(
    "memory_id",
    [
        "abc123",
        "ABC",
        "123",
        "mem_abc_123",
        "mem-abc-123",
        "a",
        "mem_abc-123_def",
    ],
    ids=[
        "alphanumeric-mixed-case",
        "alphanumeric-uppercase-only",
        "alphanumeric-digits-only",
        "with-underscore",
        "with-hyphen",
        "single-char",
        "with-underscore-and-hyphen",
    ],
)
def test_validate_memory_id_accepts_valid_ids(memory_id: str) -> None:
    """IDs matching ``^[A-Za-z0-9_\\-]+$`` are returned unchanged.

    Covers the three character classes the spec requires: alphanumeric
    (``abc123``, ``ABC``, ``123``), underscore (``mem_abc_123``), and hyphen
    (``mem-abc-123``).  The ``single-char`` row (``a``) pins the ``+``
    quantifier's minimum-length boundary (exactly one character), and the
    ``with-underscore-and-hyphen`` row (``mem_abc-123_def``) exercises both
    separators in a single ID.  The function returns the input verbatim — it
    validates only, it does not normalize.
    """
    assert _validate_memory_id(memory_id) == memory_id


@pytest.mark.parametrize(
    "memory_id",
    [
        "",
        "/",
        "a/b",
        "a\\b",
        " ",
        "a b",
        "!",
        "@",
        "#",
        ".",
        ":",
    ],
    ids=[
        "empty-string",
        "single-slash",
        "forward-slash-in-middle",
        "backslash-in-middle",
        "single-space",
        "space-in-middle",
        "exclamation",
        "at-sign",
        "hash",
        "dot",
        "colon",
    ],
)
def test_validate_memory_id_rejects_invalid_ids(memory_id: str) -> None:
    """IDs outside ``[A-Za-z0-9_\\-]`` (or the empty string) raise
    ``ValueError``.

    The empty string is rejected by the ``not memory_id`` guard in
    ``_validate_memory_id`` (``server.py:109``), which short-circuits to
    ``True`` before the regex is consulted — not by the ``+`` quantifier.  The
    ``+`` quantifier is a defense-in-depth backstop, but the public function
    cannot isolate it (the guard masks the regex's empty-string behavior), so
    this test pins the observable contract (empty string raises) rather than
    the internal mechanism.  Slashes (``/``, ``a/b``, ``a\\b``), spaces
    (`` ``, ``a b``), and special characters (``!``, ``@``, ``#``, ``.``,
    ``:``) all contain characters outside the allowed class and fail the
    regex.  This was verified empirically before writing the assertions.
    """
    with pytest.raises(ValueError, match="Invalid memory_id format"):
        _validate_memory_id(memory_id)


# ---------------------------------------------------------------------------
# Tests for ``_error`` (task 3.4)
# ---------------------------------------------------------------------------
#
# ``_error`` is a pure constructor: it builds a dict with ``error`` and
# ``detail`` keys, and adds ``status`` only when it is not ``None``.  The
# tests verify the key set explicitly (``"status" not in result`` /
# ``"status" in result``) rather than relying solely on dict equality, so a
# future change that accidentally injects a ``status: None`` key is caught.


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("http_404", "not found"),
        ("http_500", "internal server error"),
        ("invalid_memory_id", "memory_id 'a/b' is not allowed"),
        ("messages_missing", "either text or messages is required"),
    ],
    ids=[
        "http-404",
        "http-500",
        "invalid-memory-id",
        "messages-missing",
    ],
)
def test_error_without_status_omits_status_key(code: str, detail: str) -> None:
    """``_error(code, detail)`` returns ``{"error": code, "detail": detail}``
    with no ``status`` key.

    Both the dict-equality assert and an explicit ``"status" not in result``
    guard are used: the equality assert catches any extra or wrong key, and the
    membership guard documents the absence intent so a future change that
    injects ``status: None`` is caught even if dict equality is later loosened.
    """
    result = _error(code, detail)
    assert result == {"error": code, "detail": detail}
    assert "status" not in result


@pytest.mark.parametrize(
    ("code", "detail", "status"),
    [
        ("http_404", "not found", 404),
        ("http_500", "internal server error", 500),
        ("http_401", "unauthorized", 401),
        ("http_request_failed", "connection refused", 503),
        # ``status=0`` is falsy but not ``None``.  The implementation uses
        # ``if status is not None`` (``server.py:117``), so the ``status`` key
        # is kept for ``0`` — this row pins the ``is not None`` vs ``if status``
        # (truthiness) distinction, which is the core contract of ``_error``.
        # A mutant that changed the guard to ``if status:`` would drop the key
        # for ``status=0`` and fail this row (dict equality + membership).
        ("http_0", "ok", 0),
    ],
    ids=[
        "http-404",
        "http-500",
        "http-401",
        "http-request-failed",
        "status-zero-falsy-but-valid",
    ],
)
def test_error_with_status_includes_status_key(
    code: str, detail: str, status: int
) -> None:
    """``_error(code, detail, status=N)`` returns a dict with ``error``,
    ``detail``, and ``status`` keys, where ``status`` equals ``N``.

    The ``status`` key presence is asserted explicitly (``"status" in
    result``) in addition to dict equality, so the intent is documented
    independently of the full-dict comparison.  The ``status-zero-falsy-but-
    valid`` row pins the ``if status is not None`` guard against a regression
    to ``if status`` (truthiness), which would drop the ``status`` key for
    ``status=0``.
    """
    result = _error(code, detail, status=status)
    assert result == {"error": code, "detail": detail, "status": status}
    assert "status" in result


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("http_404", "not found"),
        ("http_500", "internal server error"),
    ],
    ids=[
        "http-404",
        "http-500",
    ],
)
def test_error_with_explicit_none_status_omits_status_key(
    code: str, detail: str
) -> None:
    """Passing ``status=None`` explicitly behaves identically to omitting the
    argument — no ``status`` key is present.

    This pins the ``None`` side of the ``if status is not None`` branch:
    ``None`` is the sentinel that suppresses the key, not a value that gets
    stored.  The falsy-but-not-``None`` side (e.g. ``status=0``) is covered
    separately in ``test_error_with_status_includes_status_key`` via the
    ``status-zero-falsy-but-valid`` row.
    """
    result = _error(code, detail, status=None)
    assert result == {"error": code, "detail": detail}
    assert "status" not in result


def test_error_is_pure_constructor_no_mutation() -> None:
    """``_error`` is a pure constructor: repeated calls with the same
    arguments return equal (but distinct) dicts, and mutating one does not
    affect the other.

    Two independent calls produce two distinct dict objects (``is not``) with
    equal contents (``==``), confirming no shared mutable state.  Mutating the
    first result must not change the second — this guards against a future
    change that caches and reuses a shared dict.
    """
    a = _error("http_404", "not found", status=404)
    b = _error("http_404", "not found", status=404)
    assert a == b
    assert a is not b
    # Mutating one must not affect the other.
    a["error"] = "mutated"
    assert b["error"] == "http_404"
