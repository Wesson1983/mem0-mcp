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


class _StubContext:
    """Minimal stand-in for ``mcp.server.fastmcp.Context``.

    The real ``Context`` exposes a ``session_config`` attribute (a dict or
    ``None``). ``_resolve_settings`` only touches ``ctx.session_config`` via
    ``getattr`` (``server.py:183``), so a bare attribute holder is sufficient
    and avoids constructing a real ``Context`` (which requires a live session).
    """

    def __init__(self, session_config: object) -> None:
        self.session_config = session_config


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
