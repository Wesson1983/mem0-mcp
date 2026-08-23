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

``_int_env`` (task 3.5):
- Returns the env value when set and valid (including ``0``, negative, large).
- Returns the default when the env var is unset.
- Returns the default when the env var is set to a non-integer, and logs a
  WARNING-level record naming the env var.
- Returns the default for an empty-string env value (treated as unset by the
  ``not raw`` guard).

``_with_default_filters`` (task 3.6):
- Injects ``user_id`` when absent (empty filters or filters with other keys).
- Injects ``agent_id`` when absent and ``default_agent`` is set.
- Preserves caller-supplied ``user_id`` and ``agent_id`` (caller wins over
  defaults).
- Handles ``None`` filters input (returns a fresh dict, does not raise).
- Does not inject ``agent_id`` when ``default_agent`` is ``None`` or falsy
  (the guard is ``if default_agent`` truthiness, not ``is not None``).
- Does not mutate the input ``filters`` dict (``dict(filters)`` copies it).
"""

from __future__ import annotations

import logging

import pytest

from mem0_mcp_server.server import (
    _error,
    _int_env,
    _redact,
    _validate_base_url,
    _validate_memory_id,
    _with_default_filters,
)


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


# ---------------------------------------------------------------------------
# Tests for ``_int_env`` (task 3.5)
# ---------------------------------------------------------------------------
#
# ``_int_env(name, default)`` reads ``os.getenv(name)`` at call time (not at
# import time), so ``monkeypatch.setenv`` / ``monkeypatch.delenv`` are the
# correct isolation mechanism — unlike the module-level constants
# (``ENV_API_KEY`` etc.) captured at import, which require
# ``monkeypatch.setattr``.  A unique env var name (``_INT_ENV_TEST_VAR``) is
# used to avoid colliding with any real ``MEM0_*`` variable.  Every test
# starts by deleting the var (``raising=False``) so a leaked value from a
# previous test or the host environment cannot affect the result.


_INT_ENV_TEST_VAR = "MEM0_TEST_INT_ENV"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42", 42),
        ("0", 0),
        ("-1", -1),
        ("999999", 999999),
        ("-2147483648", -2147483648),
    ],
    ids=[
        "positive",
        "zero",
        "negative",
        "large",
        "int32-min",
    ],
)
def test_int_env_returns_value_when_set_and_valid(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """A set, parseable integer env var is returned as an ``int``.

    Covers the spec's "returns the env value when set and valid" case across
    several integer shapes: positive, zero (falsy but valid — pins the
    ``int(raw)`` return path against a truthiness mutant), negative, large,
    and the 32-bit signed minimum.
    """
    monkeypatch.delenv(_INT_ENV_TEST_VAR, raising=False)
    monkeypatch.setenv(_INT_ENV_TEST_VAR, raw)
    assert _int_env(_INT_ENV_TEST_VAR, 7) == expected


def test_int_env_returns_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the env var is absent, ``_int_env`` returns the default.

    ``monkeypatch.delenv(..., raising=False)`` guarantees a clean baseline
    even if the host environment happens to define the variable, so the test
    is order-independent and environment-independent.
    """
    monkeypatch.delenv(_INT_ENV_TEST_VAR, raising=False)
    assert _int_env(_INT_ENV_TEST_VAR, 7) == 7


def test_int_env_returns_default_when_set_to_non_integer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-integer env value yields the default and logs a WARNING that
    names the env var.

    The assertion on the log record checks only the level (``WARNING``) and
    that the env var name appears in the formatted message — not the exact
    message text, which the spec does not require and which would be brittle
    against wording changes.  ``caplog.set_level`` is scoped to the
    ``mem0_mcp_server`` logger so the capture is deterministic regardless of
    the root logger level configured at import time.
    """
    monkeypatch.delenv(_INT_ENV_TEST_VAR, raising=False)
    monkeypatch.setenv(_INT_ENV_TEST_VAR, "not_a_number")
    with caplog.at_level(logging.WARNING, logger="mem0_mcp_server"):
        result = _int_env(_INT_ENV_TEST_VAR, 7)
    assert result == 7
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and _INT_ENV_TEST_VAR in r.getMessage()
    ]
    assert warnings, (
        f"expected a WARNING record naming {_INT_ENV_TEST_VAR}; "
        f"got {caplog.records!r}"
    )


def test_int_env_returns_default_for_empty_string(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty-string env value is treated as unset (``not raw`` guard) and
    returns the default.

    This is a real edge case in the implementation: ``os.getenv`` returns
    ``""`` for ``MEM0_TEST_INT_ENV=`` (set but blank), and the ``if not raw``
    branch short-circuits before ``int("")`` would raise ``ValueError``.  No
    warning is logged in this path (the empty string is silently treated as
    unset, not as a malformed value), which this test also pins by asserting
    the absence of WARNING records — a mutant that removed the ``not raw``
    guard would route ``""`` into ``int("")``, raise ``ValueError``, log a
    warning, and fail this assertion.
    """
    monkeypatch.delenv(_INT_ENV_TEST_VAR, raising=False)
    monkeypatch.setenv(_INT_ENV_TEST_VAR, "")
    with caplog.at_level(logging.WARNING, logger="mem0_mcp_server"):
        result = _int_env(_INT_ENV_TEST_VAR, 7)
    assert result == 7
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and _INT_ENV_TEST_VAR in r.getMessage()
    ]
    assert not warnings, (
        f"expected no WARNING for empty-string value; got {warnings!r}"
    )


def test_int_env_returns_default_for_whitespace_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A whitespace-only value is truthy, so it is NOT short-circuited by the
    ``not raw`` guard; it reaches ``int(" ")`` -> ``ValueError`` -> warn +
    default.

    This pins the distinction between the empty string (``""`` is falsy ->
    short-circuit, no warning) and whitespace (``" "`` is truthy -> ValueError
    -> warning).  The codebase already strips whitespace for
    ``MEM0_DEFAULT_AGENT_ID`` (``server.py:130-131``), so a maintainer applying
    the same "strip then check blank" pattern to ``_int_env`` is plausible.  A
    mutant that changed ``if not raw:`` to ``if raw is None or not raw.strip():``
    would silently treat whitespace as unset, skip the warning, and return the
    default — this test fails under that mutant (``assert warnings`` is empty)
    and passes under the current ``if not raw:`` implementation.
    """
    monkeypatch.delenv(_INT_ENV_TEST_VAR, raising=False)
    monkeypatch.setenv(_INT_ENV_TEST_VAR, "   ")
    with caplog.at_level(logging.WARNING, logger="mem0_mcp_server"):
        result = _int_env(_INT_ENV_TEST_VAR, 7)
    assert result == 7
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and _INT_ENV_TEST_VAR in r.getMessage()
    ]
    assert warnings, (
        f"expected a WARNING for whitespace-only value; got {caplog.records!r}"
    )


# ---------------------------------------------------------------------------
# Tests for ``_with_default_filters`` (task 3.6)
# ---------------------------------------------------------------------------
#
# ``_with_default_filters(filters, default_user, default_agent)`` copies the
# input (``dict(filters) if filters else {}``), injects ``user_id`` when the
# key is absent, and injects ``agent_id`` when the key is absent AND
# ``default_agent`` is truthy.  The ``agent_id`` guard is ``if default_agent``
# (truthiness), not ``if default_agent is not None`` — so ``None`` and ``""``
# both suppress injection.  Caller-supplied values always win because the
# injection is gated on ``"key" not in result``.


@pytest.mark.parametrize(
    ("filters", "default_agent", "expected"),
    [
        # Empty filters: only user_id injected (default_agent is None).
        ({}, None, {"user_id": "u-default"}),
        # Filters with other keys but no user_id: user_id injected, other keys
        # preserved.
        ({"q": "x"}, None, {"q": "x", "user_id": "u-default"}),
    ],
    ids=[
        "empty-filters-injects-user-id",
        "other-keys-no-user-id-injects-user-id",
    ],
)
def test_with_default_filters_injects_user_id_when_absent(
    filters: dict[str, object], default_agent: str | None, expected: dict[str, object]
) -> None:
    """When ``user_id`` is absent from ``filters`` it is injected with
    ``default_user``; other keys are preserved.

    ``default_agent`` is ``None`` here so ``agent_id`` is not injected — the
    ``agent_id`` injection path is covered separately below.  Both an empty
    dict and a dict with unrelated keys are exercised to confirm the
    ``"user_id" not in result`` check is key-presence-based, not emptiness-
    based.
    """
    result = _with_default_filters(filters, "u-default", default_agent)
    assert result == expected


def test_with_default_filters_injects_agent_id_when_absent_and_default_set() -> None:
    """When ``agent_id`` is absent and ``default_agent`` is set (truthy), it is
    injected alongside ``user_id``.

    This is the positive path for the ``if default_agent and "agent_id" not in
    result`` guard: both conditions are true, so both keys are injected.
    """
    result = _with_default_filters({}, "u-default", "a-default")
    assert result == {"user_id": "u-default", "agent_id": "a-default"}


def test_with_default_filters_preserves_caller_user_id() -> None:
    """A caller-supplied ``user_id`` wins over ``default_user``; ``agent_id``
    is still injected because it is absent and ``default_agent`` is set.

    This pins the ``"user_id" not in result`` guard: the caller's value is
    present, so the default is NOT overwritten.  The assertion checks the exact
    value (``"u-caller"``, not ``"u-default"``) to prove the caller won, and
    that ``agent_id`` was still injected (the two injections are independent).
    """
    result = _with_default_filters({"user_id": "u-caller"}, "u-default", "a-default")
    assert result == {"user_id": "u-caller", "agent_id": "a-default"}


def test_with_default_filters_preserves_caller_agent_id() -> None:
    """A caller-supplied ``agent_id`` wins over ``default_agent``; ``user_id``
    is still injected because it is absent.

    Symmetric to the caller-``user_id`` test: the ``"agent_id" not in result``
    guard prevents the default from overwriting the caller's value, while the
    absent ``user_id`` is still injected from ``default_user``.
    """
    result = _with_default_filters({"agent_id": "a-caller"}, "u-default", "a-default")
    assert result == {"user_id": "u-default", "agent_id": "a-caller"}


def test_with_default_filters_preserves_both_caller_values() -> None:
    """When the caller supplies both ``user_id`` and ``agent_id``, neither
    default is injected — the result equals the input (copied)."""
    filters = {"user_id": "u-caller", "agent_id": "a-caller"}
    result = _with_default_filters(filters, "u-default", "a-default")
    assert result == {"user_id": "u-caller", "agent_id": "a-caller"}


def test_with_default_filters_handles_none_filters() -> None:
    """``None`` filters does not raise; a fresh dict with only ``user_id``
    injected is returned.

    The ``dict(filters) if filters else {}`` branch handles ``None`` (falsy)
    by starting from an empty dict, so no ``TypeError`` from ``dict(None)``.
    With ``default_agent=None``, only ``user_id`` is injected.
    """
    result = _with_default_filters(None, "u-default", None)
    assert result == {"user_id": "u-default"}


def test_with_default_filters_none_filters_with_default_agent() -> None:
    """``None`` filters with a set ``default_agent`` injects both keys,
    confirming the ``None``-input path still reaches the ``agent_id`` guard."""
    result = _with_default_filters(None, "u-default", "a-default")
    assert result == {"user_id": "u-default", "agent_id": "a-default"}


@pytest.mark.parametrize(
    "default_agent",
    [None, ""],
    ids=[
        "default-agent-none",
        "default-agent-empty-string",
    ],
)
def test_with_default_filters_falsy_default_agent_does_not_inject_agent_id(
    default_agent: str | None,
) -> None:
    """A falsy ``default_agent`` (``None`` or ``""``) does NOT inject
    ``agent_id``.

    The guard is ``if default_agent`` (truthiness), not ``if default_agent is
    not None``.  ``None`` is the documented "no agent" sentinel, and ``""`` is
    falsy so it is also suppressed — a mutant that changed the guard to
    ``if default_agent is not None`` would inject ``agent_id: ""`` for the
    empty-string row and fail this assertion.
    """
    result = _with_default_filters({}, "u-default", default_agent)
    assert result == {"user_id": "u-default"}
    assert "agent_id" not in result


def test_with_default_filters_does_not_mutate_input() -> None:
    """The input ``filters`` dict is not mutated — the function copies it via
    ``dict(filters)``.

    A snapshot of the input is taken before the call and compared after;
    additionally the returned dict is a distinct object (``is not`` the input)
    so mutating the result cannot leak back into the caller's dict.  This
    guards against a mutant that dropped the ``dict(...)`` copy and mutated
    ``filters`` in place.
    """
    filters = {"q": "x", "user_id": "u-caller"}
    snapshot = dict(filters)
    result = _with_default_filters(filters, "u-default", "a-default")
    # Input is unchanged.
    assert filters == snapshot
    # Result is a distinct object, not the same dict mutated in place.
    assert result is not filters
    # Mutating the result must not affect the input.
    result["agent_id"] = "mutated"
    assert "agent_id" not in filters


def test_with_default_filters_returns_fresh_dict_for_none_input() -> None:
    """For ``None`` input the returned dict is a fresh object (not a shared
    singleton) so callers can safely mutate it without affecting anyone else.

    Two consecutive calls with ``None`` return distinct objects (``is not``),
    confirming no cached/shared empty dict is reused.
    """
    a = _with_default_filters(None, "u-default", None)
    b = _with_default_filters(None, "u-default", None)
    assert a == {"user_id": "u-default"}
    assert b == {"user_id": "u-default"}
    assert a is not b
