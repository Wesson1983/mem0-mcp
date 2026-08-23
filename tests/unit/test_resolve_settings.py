"""Unit tests for ``_resolve_settings`` env-only resolution (task 4.1).

Covers the env-only resolution path of ``_resolve_settings`` — the case where
no session config is supplied (``session_config`` is ``None``) and every field
is sourced from the module-level constants captured at import time:

- ``ENV_API_KEY`` + ``ENV_BASE_URL`` + ``ENV_DEFAULT_USER_ID`` resolve to the
  values held in those constants, and ``ENV_DEFAULT_AGENT_ID`` resolves to
  whatever the constant holds (``None`` when unset, a string when set).
- ``ENV_API_KEY = None`` raises ``RuntimeError`` — the ``not api_key`` guard
  (``server.py:191``) fires because both the session-config and env sources are
  absent/falsy, so the server cannot authenticate.

Why ``monkeypatch.setattr`` and not ``monkeypatch.setenv``:
  ``server.py`` resolves configuration into module-level constants at import
  time — ``ENV_API_KEY``, ``ENV_BASE_URL``, ``ENV_DEFAULT_USER_ID``, and
  ``ENV_DEFAULT_AGENT_ID`` are all evaluated during module execution
  (``server.py:124-140``). By the time a test runs, ``os.getenv`` has already
  been consulted, so ``monkeypatch.setenv("MEM0_API_KEY", ...)`` has no effect
  on the values ``_resolve_settings`` actually reads. The tests therefore patch
  the module attributes directly via ``monkeypatch.setattr(server, "ENV_API_KEY",
  ...)`` — this is a property of the existing code (design.md decision 7), not
  something the test suite should change.

``ctx`` handling:
  ``_resolve_settings`` reads ``session_config`` via
  ``getattr(ctx, "session_config", None)`` (``server.py:183``). Two ``ctx``
  shapes are covered:
  1. ``ctx=None`` — the tool-function default. ``getattr(None,
     "session_config", None)`` returns ``None`` without raising, so
     ``_resolve_settings(None)`` follows the env-only path.
  2. A stub ``Context`` whose ``session_config`` attribute is ``None`` — the
     shape a real MCP ``Context`` has when no Smithery session config was
     supplied. ``_config_value(None, ...)`` returns ``None`` for every field
     (``server.py:163-164``), so the env-only path is taken identically.

The return tuple order is ``(api_key, default_user, default_agent, base_url)``
(``server.py:219``); assertions unpack by name to stay robust against a future
field-order change.
"""

from __future__ import annotations

import pytest

from mem0_mcp_server import server
from mem0_mcp_server.server import _resolve_settings
from tests.conftest import StubContext

# Alias kept so the parametrize/ids and call sites below read as before; the
# stub itself now lives in ``tests/conftest.py`` (shared with the integration
# suite) so the two modules do not maintain divergent copies.
_StubContext = StubContext


class _StubSessionConfigAttrs:
    """Attribute-bearing stand-in for a session-config object.

    ``_config_value`` (``server.py:162-167``) branches on
    ``isinstance(source, dict)``: for a non-dict source it reads
    ``getattr(source, field, None)`` (guarded by ``hasattr``). This stub holds
    the four session-config fields as plain attributes so the ``getattr``
    branch is exercised — the complement to the ``dict`` shape (which hits the
    ``source.get(field)`` branch) used elsewhere in this file. Task 4.3
    explicitly requires both shapes because the helper branches on the source
    type, and a regression in either branch would otherwise go undetected.
    """

    def __init__(
        self,
        mem0_api_key: str | None,
        base_url: str | None,
        default_user_id: str | None,
        default_agent_id: str | None,
    ) -> None:
        self.mem0_api_key = mem0_api_key
        self.base_url = base_url
        self.default_user_id = default_user_id
        self.default_agent_id = default_agent_id


# ---------------------------------------------------------------------------
# Env-only resolution: all three env fields resolve to the patched constants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctx",
    [
        None,
        _StubContext(session_config=None),
    ],
    ids=[
        "ctx-none-tool-default",
        "ctx-stub-session-config-none",
    ],
)
def test_resolve_settings_env_only_uses_module_constants(
    monkeypatch: pytest.MonkeyPatch, ctx: _StubContext | None
) -> None:
    """With no session config, every field comes from the module-level
    ``ENV_*`` constants (patched via ``monkeypatch.setattr``, not ``setenv``).

    Both ``ctx=None`` (the tool-function default) and a stub ``Context`` whose
    ``session_config`` is ``None`` take the env-only path: ``getattr(None,
    "session_config", None)`` returns ``None`` without raising, and
    ``_config_value(None, ...)`` returns ``None`` for every field, so
    ``session_* or ENV_*`` collapses to ``ENV_*`` for all four fields.

    The constants are patched (not the environment) because ``server.py``
    captures them at import time (``server.py:124-140``) — ``monkeypatch.setenv``
    would have no effect on the values ``_resolve_settings`` reads. This is the
    documented behavior (design.md decision 7) and is asserted here by patching
    the attributes to values distinct from any real environment so a regression
    to ``os.getenv``-based resolution would read the wrong (unpatched) values.
    """
    # Patch the module-level constants captured at import time. Use sentinel
    # values unlikely to match the real environment so a regression that reads
    # os.getenv instead of the constants would produce different values.
    monkeypatch.setattr(server, "ENV_API_KEY", "test-env-api-key")
    monkeypatch.setattr(server, "ENV_BASE_URL", "http://localhost:9999")
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", "test-env-user")
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", "test-env-agent")

    # ``ctx`` is a duck-typed stub (not a real ``Context``); the call is
    # intentional, so silence mypy's structural-mismatch error.
    api_key, default_user, default_agent, base_url = _resolve_settings(ctx)  # type: ignore[arg-type]

    assert api_key == "test-env-api-key"
    assert default_user == "test-env-user"
    assert default_agent == "test-env-agent"
    # ``_validate_base_url`` strips a trailing slash but does not otherwise
    # mutate an already-clean URL, so the patched value is returned verbatim.
    assert base_url == "http://localhost:9999"


def test_resolve_settings_env_only_default_agent_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``ENV_DEFAULT_AGENT_ID`` is ``None`` (unset), the resolved
    ``default_agent`` is ``None`` — preserving the pre-change behavior of not
    injecting an ``agent_id`` (callers' ``agent_id or default_agent`` yields
    ``None`` and is dropped by ``exclude_none=True``).

    This is the env-only path with the agent env var unset. ``ctx=None`` is
    used (the tool default) to also exercise the ``getattr(None,
    "session_config", None)`` -> ``None`` path here, not just in the all-set
    case above.
    """
    monkeypatch.setattr(server, "ENV_API_KEY", "test-env-api-key")
    monkeypatch.setattr(server, "ENV_BASE_URL", "http://localhost:9999")
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", "test-env-user")
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", None)

    api_key, default_user, default_agent, base_url = _resolve_settings(None)

    assert api_key == "test-env-api-key"
    assert default_user == "test-env-user"
    assert default_agent is None
    assert base_url == "http://localhost:9999"


def test_resolve_settings_strips_trailing_slash_from_env_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_settings`` runs the env ``base_url`` through
    ``_validate_base_url`` (``server.py:218``), which strips a trailing slash.
    A patched ``ENV_BASE_URL`` with a trailing slash is returned without it.
    """
    monkeypatch.setattr(server, "ENV_API_KEY", "test-env-api-key")
    monkeypatch.setattr(server, "ENV_BASE_URL", "http://localhost:9999/")
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", "test-env-user")
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", None)

    _api_key, _default_user, _default_agent, base_url = _resolve_settings(None)

    assert base_url == "http://localhost:9999"


# ---------------------------------------------------------------------------
# RuntimeError when ENV_API_KEY is None (and no session config supplies one)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctx",
    [
        None,
        _StubContext(session_config=None),
    ],
    ids=[
        "ctx-none-tool-default",
        "ctx-stub-session-config-none",
    ],
)
def test_resolve_settings_raises_runtime_error_when_env_api_key_none(
    monkeypatch: pytest.MonkeyPatch, ctx: _StubContext | None
) -> None:
    """When ``ENV_API_KEY`` is ``None`` and no session config supplies an
    API key, ``_resolve_settings`` raises ``RuntimeError``.

    The guard is ``if not api_key`` (``server.py:191``), where
    ``api_key = session_api_key or ENV_API_KEY``. With ``session_config`` is
    ``None``, ``session_api_key`` is ``None``; with ``ENV_API_KEY`` patched to
    ``None``, ``api_key`` is ``None`` (falsy), so the ``RuntimeError`` fires.

    Both ``ctx=None`` and a stub ``Context`` with ``session_config=None`` are
    covered: the env-only path is taken in both cases, so both must raise. The
    match string checks for ``MEM0_API_KEY is required`` so a regression that
    changed the error message would be caught.
    """
    monkeypatch.setattr(server, "ENV_API_KEY", None)
    monkeypatch.setattr(server, "ENV_BASE_URL", "http://localhost:9999")
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", "test-env-user")
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", None)

    with pytest.raises(RuntimeError, match="MEM0_API_KEY is required"):
        # ``ctx`` is a duck-typed stub (not a real ``Context``); the call is
        # intentional, so silence mypy's structural-mismatch error.
        _resolve_settings(ctx)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Built-in default for ``ENV_DEFAULT_USER_ID`` flows through when unset
# ---------------------------------------------------------------------------


def test_resolve_settings_env_only_uses_builtin_default_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``ENV_DEFAULT_USER_ID`` is left unpatched (i.e. at its real
    import-time value), the resolved ``default_user`` equals that constant —
    pinning the built-in ``"mem0-mcp"`` default that ``server.py:126`` sets via
    ``os.getenv("MEM0_DEFAULT_USER_ID", "mem0-mcp")``.

    Every other test in this file patches ``ENV_DEFAULT_USER_ID`` to
    ``"test-env-user"``, which proves the constant is *read* but not that the
    built-in default flows through when ``MEM0_DEFAULT_USER_ID`` is unset in
    the environment. This test patches only ``ENV_API_KEY``,
    ``ENV_BASE_URL``, and ``ENV_DEFAULT_AGENT_ID``, leaving
    ``ENV_DEFAULT_USER_ID`` at whatever value the module captured at import
    time.

    Why snapshot before patching: the host environment running this test may
    or may not have ``MEM0_DEFAULT_USER_ID`` set. If it is set,
    ``server.ENV_DEFAULT_USER_ID`` holds that value (not ``"mem0-mcp"``); if
    unset, it holds the built-in ``"mem0-mcp"``. Capturing the constant into a
    local variable *before* any ``monkeypatch.setattr`` call makes the
    assertion deterministic regardless of the host environment — we assert
    against the snapshot, not against a hard-coded ``"mem0-mcp"`` literal,
    which would fail on a host that has the env var set. The behavior being
    pinned is: ``_resolve_settings`` reads ``ENV_DEFAULT_USER_ID`` verbatim
    (the unpatched import-time constant), so the built-in default reaches the
    caller unchanged when no env override is present.
    """
    # Snapshot the real import-time constant BEFORE patching anything, so the
    # assertion is independent of whether the host env has
    # ``MEM0_DEFAULT_USER_ID`` set (it would be ``"mem0-mcp"`` when unset, or
    # the operator's value when set).
    builtin_default_user_id = server.ENV_DEFAULT_USER_ID

    # Patch every constant EXCEPT ``ENV_DEFAULT_USER_ID`` so the built-in
    # default is the one that flows through ``default_user = session_default_user
    # or ENV_DEFAULT_USER_ID`` (``server.py:203``).
    monkeypatch.setattr(server, "ENV_API_KEY", "test-env-api-key")
    monkeypatch.setattr(server, "ENV_BASE_URL", "http://localhost:9999")
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", None)

    _api_key, default_user, _default_agent, _base_url = _resolve_settings(None)

    # The resolved ``default_user`` must equal the unpatched import-time
    # constant — i.e. the built-in default (``"mem0-mcp"`` on a clean host)
    # flows through verbatim.
    assert default_user == builtin_default_user_id


# ---------------------------------------------------------------------------
# Env-over-session-config precedence (task 4.2)
#
# When both an env constant and a session-config value are set for the same
# field, the env value wins and the session-config value is dropped (with a
# warning). The resolved tuple must therefore hold the env values, NOT the
# session-config values. Per the design non-goals (design.md:187-190), warning
# log text is NOT asserted here — only the resolved values.
#
# Two test shapes are used:
# 1. ``test_resolve_settings_env_wins_over_session_config_all_fields`` — a
#    single call with all four fields conflicting at once. This is the
#    realistic shape (an operator sets all env vars and a Smithery session
#    config also supplies all four) and proves the precedence holds for every
#    field in one ``_resolve_settings`` invocation.
# 2. ``test_resolve_settings_env_wins_over_session_config_per_field`` —
#    parametrized across the four fields so a regression that broke precedence
#    for exactly one field (e.g. a typo in one ``if session_* and ENV_*``
#    guard) is isolated to a single failing case rather than one combined
#    failure.
#
# Session-config shape: only the ``dict`` shape is exercised here.
# ``_config_value`` (``server.py:162-167``) also branches on
# ``isinstance(source, dict)`` vs ``getattr`` for an object-with-attributes
# shape, but Task 4.3 explicitly covers the session-config fallback with both
# shapes, so the object-attribute shape is left to 4.3 to avoid duplicating
# that coverage here.
# ---------------------------------------------------------------------------


def test_resolve_settings_env_wins_over_session_config_all_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env and session config are both set for all four fields, every
    resolved value is the env value — the session-config values are dropped.

    ``_resolve_settings`` applies the same precedence pattern to each field
    (``server.py:185-218``): when both the session-config value and the env
    constant are truthy, the session-config value is set to ``None`` (with a
    warning) and ``session_* or ENV_*`` collapses to ``ENV_*``. This test sets
    all four env constants AND all four session-config keys to *distinct*
    values so that "env wins" is unambiguous — if the session-config value
    leaked through, the assertion would fail against a different literal.

    A single combined call is used (rather than one test per field) because it
    mirrors the realistic operator scenario — env vars set globally and a
    Smithery session config also supplying all four — and proves precedence
    holds for every field in one ``_resolve_settings`` invocation. Per-field
    isolation is provided by the parametrized companion test below.

    Per the design non-goals (design.md:187-190), the warning log text is NOT
    asserted — only the resolved tuple values. ``caplog`` assertions on the
    warning text are a documented future addition, not a goal of this task.
    """
    # Env values — the values that must win. The base URL uses ``localhost``
    # (a ``_LOCAL_HOSTS`` entry, ``server.py:75``) so ``_validate_base_url``
    # accepts plain HTTP; the port is distinct from the session-config port so
    # the two URLs remain distinguishable after validation.
    monkeypatch.setattr(server, "ENV_API_KEY", "env-api-key-wins")
    monkeypatch.setattr(server, "ENV_BASE_URL", "http://localhost:7777")
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", "env-user-wins")
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", "env-agent-wins")

    # Session-config values — distinct from the env values so a leak is
    # detectable. Every key is set so all four ``if session_* and ENV_*``
    # conflict guards (``server.py:185,197,205,213``) fire. The session
    # ``base_url`` uses a different localhost port so it is distinguishable
    # from the env base URL.
    session_config = {
        "mem0_api_key": "session-api-key-loses",
        "base_url": "http://localhost:8888",
        "default_user_id": "session-user-loses",
        "default_agent_id": "session-agent-loses",
    }
    ctx = _StubContext(session_config=session_config)

    # ``ctx`` is a duck-typed stub (not a real ``Context``); the call is
    # intentional, so silence mypy's structural-mismatch error.
    api_key, default_user, default_agent, base_url = _resolve_settings(ctx)  # type: ignore[arg-type]

    # Each resolved value must be the env value, NOT the session-config value.
    assert api_key == "env-api-key-wins"
    assert api_key != "session-api-key-loses"
    assert default_user == "env-user-wins"
    assert default_user != "session-user-loses"
    assert default_agent == "env-agent-wins"
    assert default_agent != "session-agent-loses"
    # ``_validate_base_url`` strips a trailing slash but does not otherwise
    # mutate a clean URL, so the env base URL is returned verbatim.
    assert base_url == "http://localhost:7777"
    assert base_url != "http://localhost:8888"


@pytest.mark.parametrize(
    ("env_attr", "session_config_key", "env_value", "session_value", "index"),
    [
        ("ENV_API_KEY", "mem0_api_key", "env-key-only", "sess-key-only", 0),
        ("ENV_BASE_URL", "base_url", "http://localhost:7001", "http://localhost:7002", 3),
        ("ENV_DEFAULT_USER_ID", "default_user_id", "env-user-only", "sess-user-only", 1),
        ("ENV_DEFAULT_AGENT_ID", "default_agent_id", "env-agent-only", "sess-agent-only", 2),
    ],
    ids=["api_key", "base_url", "default_user_id", "default_agent_id"],
)
def test_resolve_settings_env_wins_over_session_config_per_field(
    monkeypatch: pytest.MonkeyPatch,
    env_attr: str,
    session_config_key: str,
    env_value: str,
    session_value: str,
    index: int,
) -> None:
    """For each field individually, when both env and session-config are set,
    the env value wins for that field.

    This is the per-field companion to the combined test above. Only the field
    under test conflicts; the other three env constants are set to neutral
    sentinels and their session-config keys are omitted, so the only
    precedence decision under test is the one for ``env_attr``. This isolates a
    regression that broke precedence for exactly one field (e.g. a typo in one
    ``if session_* and ENV_*`` guard at ``server.py:185,197,205,213``) to a
    single failing case.

    ``index`` is the position of the field in the returned tuple
    ``(api_key, default_user, default_agent, base_url)`` (``server.py:219``);
    the assertion unpacks by index so each parametrized case checks only its
    own field. The other tuple positions are not asserted here — they are
    covered by the combined test and by task 4.1's env-only tests.

    Per the design non-goals (design.md:187-190), the warning log text is NOT
    asserted — only the resolved value for the field under test.
    """
    # Patch the env constant under test to its winning value.
    monkeypatch.setattr(server, env_attr, env_value)
    # Set the other three env constants to neutral sentinels so they do not
    # interfere with the field under test (and so ``ENV_API_KEY`` is never
    # ``None``, which would raise ``RuntimeError`` before the field under test
    # is resolved).
    if env_attr != "ENV_API_KEY":
        monkeypatch.setattr(server, "ENV_API_KEY", "neutral-api-key")
    if env_attr != "ENV_BASE_URL":
        monkeypatch.setattr(server, "ENV_BASE_URL", "http://localhost:9999")
    if env_attr != "ENV_DEFAULT_USER_ID":
        monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", "neutral-user")
    if env_attr != "ENV_DEFAULT_AGENT_ID":
        monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", "neutral-agent")

    # Only the field under test is present in the session config, with a value
    # distinct from the env value so a leak is detectable.
    session_config = {session_config_key: session_value}
    ctx = _StubContext(session_config=session_config)

    # ``ctx`` is a duck-typed stub (not a real ``Context``); the call is
    # intentional, so silence mypy's structural-mismatch error.
    result = _resolve_settings(ctx)  # type: ignore[arg-type]

    # The resolved value at ``index`` must be the env value, not the
    # session-config value.
    assert result[index] == env_value
    assert result[index] != session_value


# ---------------------------------------------------------------------------
# Session-config fallback (task 4.3)
#
# With ``ENV_API_KEY`` / ``ENV_BASE_URL`` / ``ENV_DEFAULT_AGENT_ID`` patched to
# ``None``, the session-config values are used for those three fields — the
# ``if session_* and ENV_*`` conflict guards (``server.py:185,205,213``) do not
# fire because the env constant is falsy, so ``session_* or ENV_*`` collapses to
# the session-config value.
#
# ``ENV_DEFAULT_USER_ID`` is NOT patched to ``None``: ``server.py:126`` resolves
# it via ``os.getenv("MEM0_DEFAULT_USER_ID", "mem0-mcp")``, so the constant
# always carries at least the built-in ``"mem0-mcp"`` default and is never
# empty. The conflict guard at ``server.py:197`` therefore ALWAYS fires when a
# session-config ``default_user_id`` is set, dropping the session-config value
# (with a warning) so ``default_user = session_default_user or
# ENV_DEFAULT_USER_ID`` resolves to ``ENV_DEFAULT_USER_ID``. A session-config
# ``default_user_id`` is in practice always overridden — operators must set
# ``MEM0_DEFAULT_USER_ID`` to change the default user. This test asserts that
# documented always-overridden behavior (env wins) rather than a fallback.
#
# Both session-config shapes ``_config_value`` supports are covered
# (``server.py:162-167``):
# 1. A plain ``dict`` — ``_config_value`` returns ``source.get(field)``.
# 2. An object with attributes (``_StubSessionConfigAttrs``) — ``_config_value``
#    returns ``getattr(source, field, None)``.
# Parametrizing the fallback test across both shapes ensures a regression in
# either ``_config_value`` branch is caught.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_config",
    [
        # dict shape — ``_config_value`` takes the ``isinstance(source, dict)``
        # branch and returns ``source.get(field)`` (``server.py:165-166``).
        {
            "mem0_api_key": "session-api-key-fallback",
            "base_url": "http://localhost:6666",
            "default_user_id": "session-user-fallback",
            "default_agent_id": "session-agent-fallback",
        },
        # attrs-object shape — ``_config_value`` takes the ``getattr`` branch
        # and returns ``getattr(source, field, None)`` (``server.py:167``).
        _StubSessionConfigAttrs(
            mem0_api_key="session-api-key-fallback",
            base_url="http://localhost:6666",
            default_user_id="session-user-fallback",
            default_agent_id="session-agent-fallback",
        ),
    ],
    ids=["dict-shape", "attrs-shape"],
)
def test_resolve_settings_session_config_fallback_when_env_none(
    monkeypatch: pytest.MonkeyPatch,
    session_config: object,
) -> None:
    """With ``ENV_API_KEY`` / ``ENV_BASE_URL`` / ``ENV_DEFAULT_AGENT_ID``
    patched to ``None``, the session-config values are used for those three
    fields; the session-config ``default_user_id`` is always overridden by
    ``ENV_DEFAULT_USER_ID`` (env wins).

    The three ``None``-patched env constants make the conflict guards at
    ``server.py:185,205,213`` skip (the ``and ENV_*`` operand is falsy), so the
    session-config values flow through ``session_* or ENV_*`` unchanged:
    ``api_key``, ``base_url``, and ``default_agent`` resolve to their
    session-config values. No ``RuntimeError`` is raised because
    ``api_key = session_api_key or ENV_API_KEY`` is truthy
    (``session_api_key`` is set).

    ``ENV_DEFAULT_USER_ID`` is deliberately NOT patched to ``None``: it carries
    the built-in ``"mem0-mcp"`` default (``server.py:126``), so the conflict
    guard at ``server.py:197`` always fires when a session-config
    ``default_user_id`` is present — the session-config value is dropped and
    ``default_user`` resolves to ``ENV_DEFAULT_USER_ID``. This is the documented
    "always overridden" behavior (AGENTS.md, ``server.py:178-181``): operators
    must set ``MEM0_DEFAULT_USER_ID`` to change the default user; a
    session-config ``default_user_id`` cannot override it. The test asserts env
    wins (the resolved ``default_user`` equals the unpatched import-time
    constant, NOT the session-config value) rather than a fallback.

    The session-config ``base_url`` uses ``http://localhost:6666`` —
    ``localhost`` is in ``_LOCAL_HOSTS`` (``server.py:75``), so
    ``_validate_base_url`` (``server.py:78-86``) accepts plain HTTP and the
    fallback URL survives validation unchanged.

    Parametrized across both ``_config_value`` shapes (dict vs attrs-object) so
    a regression in either branch of ``server.py:162-167`` is caught: the dict
    case exercises ``source.get(field)`` and the attrs case exercises
    ``getattr(source, field, None)``.
    """
    # Snapshot the real import-time constant BEFORE patching anything, so the
    # ``default_user`` assertion is independent of whether the host env has
    # ``MEM0_DEFAULT_USER_ID`` set (it would be ``"mem0-mcp"`` when unset, or
    # the operator's value when set). The behavior under test is "env wins over
    # session-config default_user_id", not a specific literal.
    builtin_default_user_id = server.ENV_DEFAULT_USER_ID

    # Patch the three env constants that SHOULD fall back to session config.
    # ``ENV_DEFAULT_USER_ID`` is intentionally left unpatched — it carries the
    # built-in default and must win over the session-config value.
    monkeypatch.setattr(server, "ENV_API_KEY", None)
    monkeypatch.setattr(server, "ENV_BASE_URL", None)
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", None)

    ctx = _StubContext(session_config=session_config)

    # ``ctx`` is a duck-typed stub (not a real ``Context``); the call is
    # intentional, so silence mypy's structural-mismatch error.
    api_key, default_user, default_agent, base_url = _resolve_settings(ctx)  # type: ignore[arg-type]

    # The three None-patched fields fall back to their session-config values.
    # These assertions hold for BOTH the dict and attrs-object shapes —
    # ``_config_value`` returns the same value via either branch.
    assert api_key == "session-api-key-fallback"
    assert base_url == "http://localhost:6666"
    assert default_agent == "session-agent-fallback"

    # ``ENV_DEFAULT_USER_ID`` always wins over the session-config
    # ``default_user_id`` (the conflict guard at ``server.py:197`` fires
    # because the env constant is never empty). Assert the documented
    # always-overridden behavior: the resolved ``default_user`` is the env
    # value, NOT the session-config ``"session-user-fallback"``.
    assert default_user == builtin_default_user_id
    assert default_user != "session-user-fallback"
