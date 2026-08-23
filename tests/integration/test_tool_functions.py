"""Integration tests for the 10 MCP tool functions (change: test-suite-foundation).

The tool functions (``add_memory``, ``search_memories``, ``get_memories``,
``delete_all_memories``, ``list_entities``, ``get_memory``, ``get_memory_history``,
``update_memory``, ``delete_memory``, ``delete_entities``) are closures defined
inside ``create_server()`` (the ``@smithery.server``-decorated factory) and are therefore NOT importable
module attributes. To call them directly from tests we instantiate the
``FastMCP`` server via ``create_server()`` and extract the underlying callables
from its tool manager's tool registry (``server._tool_manager._tools[name].fn``)
— each ``Tool`` object holds the original function the ``@server.tool``
decorator wrapped. Calling ``.fn`` directly bypasses the MCP transport layer
and the FastMCP argument-coercion/JSON-RPC framing, exercising the tool body's
own validation, settings resolution, and ``Mem0OSSClient`` dispatch end to end.

Configuration is pointed at the fake mem0 OSS server (the ``fake_mem0_server``
fixture from ``tests/conftest.py``) by patching the module-level constants
``ENV_BASE_URL`` and ``ENV_API_KEY`` via ``monkeypatch.setattr`` — NOT
``monkeypatch.setenv``, because ``server.py`` captures these at import time
(``ENV_API_KEY`` / ``ENV_BASE_URL`` / ``ENV_DEFAULT_USER_ID`` / ``ENV_DEFAULT_AGENT_ID``; design.md decision 7). ``_CLIENT_CACHE`` is cleared
before and after every test so a stale cached client bound to a previous test's
torn-down fake server is never reused.

Coverage by task (see ``openspec/changes/test-suite-foundation/tasks.md``):

- 8.1: ``add_memory`` happy path (returns the canned write JSON).
- 8.2: happy paths for the remaining 9 tool functions.
- 8.3: error-path validation (``messages_missing``, ``nothing_to_update``,
  ``scope_missing``, ``invalid_memory_id``, ``invalid_messages``).
- 8.4: default user/agent injection (asserted via the fake server's
  ``received`` echo).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from mem0_mcp_server import server
from mem0_mcp_server.server import clear_client_cache, create_server
from tests.conftest import FakeMem0Config

# The 10 tool callables are extracted from the inner ``FastMCP``'s tool
# registry (``ToolManager._tools[name].fn``). ``create_server()`` is decorated
# with ``@smithery.server(...)`` and returns a ``SmitheryFastMCP`` wrapper that
# does NOT subclass ``FastMCP`` (mro ``SmitheryFastMCP -> object``); it holds
# the real ``FastMCP`` as ``_fastmcp``. Reaching ``_tool_manager`` through
# ``_fastmcp`` keeps us on one layer of private API (FastMCP's own
# ``ToolManager._tools``, stable under the ``mcp[cli]<2.0.0`` pin) instead of
# two (smithery's wrapper + FastMCP). The guard below turns a future smithery
# or mcp release that reshapes either layer into a clear, actionable error
# instead of an opaque ``AttributeError`` mid-suite.
_EXPECTED_TOOL_NAMES = frozenset({
    "add_memory", "search_memories", "get_memories", "delete_all_memories",
    "list_entities", "get_memory", "get_memory_history", "update_memory",
    "delete_memory", "delete_entities",
})


def _extract_tool_callables(wrapped_server: Any) -> dict[str, Any]:
    """Pull the 10 underlying tool callables from the inner ``FastMCP``.

    ``wrapped_server`` is the value returned by ``create_server()`` — a
    ``SmitheryFastMCP`` whose ``_fastmcp`` attribute is the real ``FastMCP``
    instance. Each ``Tool`` in the registry exposes ``.fn`` (the original
    function the ``@server.tool`` decorator wrapped), which we call directly
    to bypass the MCP transport layer and FastMCP argument coercion.
    """
    inner = getattr(wrapped_server, "_fastmcp", None)
    if not isinstance(inner, FastMCP):
        raise TypeError(
            f"create_server() did not return a SmitheryFastMCP wrapping a "
            f"FastMCP (got {type(wrapped_server).__name__}; "
            f"expected a SmitheryFastMCP with a ._fastmcp FastMCP attribute). "
            f"The smithery>=0.4.2 wrapper layout may have changed — pin or "
            f"update smithery and revisit this extraction."
        )
    tool_manager = getattr(inner, "_tool_manager", None)
    tools_map = getattr(tool_manager, "_tools", None) if tool_manager is not None else None
    if not isinstance(tools_map, dict):
        raise TypeError(
            f"Inner FastMCP._tool_manager._tools is not a dict "
            f"(got {type(tools_map).__name__}). The mcp[cli]<2.0.0 internal "
            f"ToolManager layout may have changed — pin or update mcp and "
            f"revisit this extraction."
        )
    missing = _EXPECTED_TOOL_NAMES - set(tools_map)
    if missing:
        raise RuntimeError(
            f"Tool registry is missing expected tools: {sorted(missing)}. "
            f"Got: {sorted(tools_map)}."
        )
    return {name: tool.fn for name, tool in tools_map.items()}

_API_KEY = "test-tool-functions-api-key-aaaaaaaa"


class _StubContext:
    """Minimal stand-in for ``mcp.server.fastmcp.Context``.

    The tool functions only touch ``ctx`` via ``getattr(ctx, "session_config",
    None)`` inside ``_resolve_settings`` (its ``session_config = getattr(ctx,
    "session_config", None)`` line). A bare attribute
    holder with ``session_config = None`` is sufficient and avoids constructing
    a real ``Context`` (which requires a live MCP session). Mirrors the stub in
    ``tests/unit/test_resolve_settings.py``.
    """

    def __init__(self, session_config: object = None) -> None:
        self.session_config = session_config


# Sentinel ``Context`` reused across the happy-path tests. ``session_config`` is
# ``None`` so ``_resolve_settings`` follows the env-only path and reads the
# patched module constants.
_STUB_CTX = _StubContext(session_config=None)


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterator[None]:
    """Clear ``_CLIENT_CACHE`` before and after every test in this module.

    ``_CLIENT_CACHE`` is module-level mutable state shared across the whole
    process. A tool call resolves a client via ``_client(base_url, api_key)``
    (the ``_client`` helper) keyed by ``(base_url, sha256(api_key)[:16])``; without
    clearing, a client cached against a previous test's fake-server port would
    be reused after that server tore down, producing
    ``http_request_failed`` errors. Clearing before *and* after each test keeps
    each test isolated and deterministic regardless of pass/fail. Mirrors the
    fixture in ``tests/integration/test_mem0_oss_client.py``.
    """
    clear_client_cache()
    yield
    clear_client_cache()


@pytest.fixture
def tool_functions(
    fake_mem0_server: tuple[str, FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], FakeMem0Config]:
    """Build the MCP server and extract the 10 tool callables.

    Patches the module-level ``ENV_BASE_URL`` / ``ENV_API_KEY`` constants
    (captured at import time) so ``_resolve_settings`` resolves to the fake
    server's URL and the test API key. ``ENV_API_KEY`` must be set or
    ``create_server()`` only logs a warning (it does not raise), but every
    tool call would then hit the ``not api_key`` guard in ``_resolve_settings``
    and raise ``RuntimeError`` — patching it here makes the happy-path calls
    succeed.

    Returns ``(tools, config)`` where ``tools`` maps tool name to the underlying
    callable (``Tool.fn``) and ``config`` is the fake server's mutable config
    (so tests can override ``responses`` per-test and read ``received`` for the
    echo assertions in task 8.4). Tool extraction goes through
    :func:`_extract_tool_callables`, which reaches the inner ``FastMCP`` via
    the ``SmitheryFastMCP._fastmcp`` attribute and fails with a clear message
    if the smithery/mcp wrapper layout changes.
    """
    base_url, config = fake_mem0_server
    monkeypatch.setattr(server, "ENV_BASE_URL", base_url)
    monkeypatch.setattr(server, "ENV_API_KEY", _API_KEY)

    wrapped = create_server()
    tools = _extract_tool_callables(wrapped)
    return tools, config


# ---------------------------------------------------------------------------
# Task 8.1 — ``add_memory`` happy path
# ---------------------------------------------------------------------------


def test_add_memory_returns_canned_write_response(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``add_memory(text="...")`` returns the canned write JSON.

    Calls the ``add_memory`` tool callable directly with a stub ``Context`` and
    asserts the response matches the fake server's canned ``POST /memories``
    body (``{"results": [{"id": "mem-1", "memory": "likes pizza", "event":
    "ADD"}]}``). The fake server merges a ``_received`` echo into dict bodies
    (task 2.1 echo contract), so the assertion checks the canned content is
    present and no ``error`` key was produced — the same pattern used by the
    wrapper-level happy-path tests in ``test_mem0_oss_client.py`` (task 7.6).
    """
    tools, _config = tool_functions

    result = tools["add_memory"](text="likes pizza", ctx=_STUB_CTX)

    assert "error" not in result
    assert result["results"] == [
        {"id": "mem-1", "memory": "likes pizza", "event": "ADD"}
    ]


# ---------------------------------------------------------------------------
# Task 8.2 — happy-path tests for the remaining 9 tool functions
# ---------------------------------------------------------------------------
#
# Each tool callable is invoked directly with valid args and a stub
# ``Context``; the fake server returns its canned response for the matching
# route (see ``_default_responses`` in ``tests/conftest.py``). The fake server
# merges a ``_received`` echo into dict bodies, so assertions check the canned
# content is present and no ``error`` key was produced — the same pattern used
# by the wrapper-level happy-path tests (task 7.6).
#
# ``ctx=_STUB_CTX`` is passed explicitly because the tool functions declare
# ``ctx: Context | None = None`` as a regular parameter; without it, FastMCP's
# argument coercion would inject a real ``Context`` when called over the wire,
# but here we call ``.fn`` directly so the default ``None`` would be used.
# ``_STUB_CTX.session_config`` is ``None`` so ``_resolve_settings`` follows the
# env-only path and reads the patched ``ENV_BASE_URL`` / ``ENV_API_KEY``.
#
# Tools that take a scope (``get_memories``, ``delete_all_memories``) rely on
# ``_resolve_settings``'s ``default_user`` (the unpatched ``ENV_DEFAULT_USER_ID``,
# ``"mem0-mcp"`` on a clean host) when the caller omits ``user_id`` — the fake
# server ignores the value and returns the canned body, so the happy path is
# independent of the host environment.


def test_search_memories_returns_canned_search_response(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``search_memories(query="...")`` → POST /search → canned search result."""
    tools, _config = tool_functions

    result = tools["search_memories"](query="pizza", ctx=_STUB_CTX)

    assert "error" not in result
    assert result["results"] == [
        {"id": "mem-1", "memory": "likes pizza", "score": 0.95}
    ]


def test_get_memories_returns_canned_list_response(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``get_memories()`` → GET /memories → canned list."""
    tools, _config = tool_functions

    result = tools["get_memories"](ctx=_STUB_CTX)

    assert "error" not in result
    assert result["results"] == [
        {"id": "mem-1", "memory": "likes pizza", "user_id": "u"}
    ]


def test_delete_all_memories_returns_canned_bulk_delete_response(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``delete_all_memories()`` → DELETE /memories → canned bulk-delete result."""
    tools, _config = tool_functions

    result = tools["delete_all_memories"](ctx=_STUB_CTX)

    assert "error" not in result
    assert result["message"] == "All memories deleted"


def test_list_entities_returns_canned_entity_list(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``list_entities()`` → GET /entities → canned entity list."""
    tools, _config = tool_functions

    result = tools["list_entities"](ctx=_STUB_CTX)

    assert "error" not in result
    assert result["results"] == []


def test_get_memory_returns_canned_single_memory(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``get_memory(memory_id="mem-1")`` → GET /memories/mem-1 → canned memory."""
    tools, _config = tool_functions

    result = tools["get_memory"](memory_id="mem-1", ctx=_STUB_CTX)

    assert "error" not in result
    assert result["id"] == "mem-1"
    assert result["memory"] == "likes pizza"
    assert result["user_id"] == "u"


def test_get_memory_history_returns_canned_history(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``get_memory_history(memory_id="mem-1")`` → GET /memories/mem-1/history → canned history."""
    tools, _config = tool_functions

    result = tools["get_memory_history"](memory_id="mem-1", ctx=_STUB_CTX)

    assert "error" not in result
    assert result["results"] == [
        {"id": "mem-1", "memory": "likes pizza", "event": "ADD", "previous_memory": None}
    ]


def test_update_memory_returns_canned_update_response(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``update_memory(memory_id="mem-1", text="...")`` → PUT /memories/mem-1 → canned update."""
    tools, _config = tool_functions

    result = tools["update_memory"](memory_id="mem-1", text="updated", ctx=_STUB_CTX)

    assert "error" not in result
    assert result["id"] == "mem-1"
    assert result["memory"] == "updated text"
    assert result["event"] == "UPDATE"


def test_delete_memory_returns_canned_delete_response(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``delete_memory(memory_id="mem-1")`` → DELETE /memories/mem-1 → canned delete."""
    tools, _config = tool_functions

    result = tools["delete_memory"](memory_id="mem-1", ctx=_STUB_CTX)

    assert "error" not in result
    assert result["id"] == "mem-1"
    assert result["event"] == "DELETE"


def test_delete_entities_returns_canned_entity_delete_response(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``delete_entities(user_id="u")`` → DELETE /entities/user/u → canned result."""
    tools, _config = tool_functions

    result = tools["delete_entities"](user_id="u", ctx=_STUB_CTX)

    assert "error" not in result
    assert result["message"] == "Entity deleted"


# ---------------------------------------------------------------------------
# Task 8.3 — error-path tests for tool-level validation
# ---------------------------------------------------------------------------
#
# These tests exercise the validation guards inside the tool bodies *before*
# any HTTP call is made (or, for ``update_memory``'s ``invalid_memory_id``
# case, after the ``nothing_to_update`` guard but before the HTTP call). The
# fake server is still wired up via the ``tool_functions`` fixture so the
# settings-resolution path (``_resolve_settings``) succeeds and the tool
# reaches its validation guard; the canned responses are not asserted here
# because the tools return an ``_error(...)`` dict without hitting the wire.
#
# Error codes asserted (matching ``server.py``):
# - ``messages_missing``  — ``add_memory`` with neither ``text`` nor ``messages``
#   (the ``if not conversation:`` guard after popping ``messages``/``text``).
# - ``invalid_messages``  — ``add_memory`` with a malformed ``messages`` entry
#   (``ToolMessage`` ``ValidationError`` -> the ``except ValidationError`` branch
#   that returns ``invalid_messages``).
# - ``nothing_to_update`` — ``update_memory`` with no updatable fields
#   (the ``if not body:`` guard after ``UpdateMemoryArgs.model_dump``).
# - ``scope_missing``     — ``delete_entities`` with no user/agent/run
#   (the ``if scope is None:`` guard after the ``next(...)`` over scope tuples).
# - ``invalid_memory_id`` — ``get_memory`` / ``delete_memory`` /
#   ``get_memory_history`` / ``update_memory`` with a memory_id that fails
#   ``_validate_memory_id`` (each tool's ``except ValueError`` branch maps it to
#   ``invalid_memory_id``).
#
# Each error dict is ``{"error": <code>, "detail": <str>}`` (no ``status`` key
# — these are validation errors, not HTTP errors). The assertions check the
# ``error`` code exactly and that ``detail`` is a non-empty string, and that
# no ``status`` key leaks in (a regression that added ``status`` would change
# the contract these tools expose to MCP clients).
#
# Invalid memory_id choice: ``"bad/id"`` contains a slash, which
# ``_validate_memory_id`` rejects (``_MEMORY_ID_RE = ^[A-Za-z0-9_\\-]+$``).
# A slash is the canonical dangerous character because it
# would break the URL path (``/memories/bad/id`` -> two path segments).

_INVALID_MEMORY_ID = "bad/id"


def test_add_memory_without_text_or_messages_returns_messages_missing(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``add_memory()`` with neither ``text`` nor ``messages`` returns
    ``_error("messages_missing", ...)``.

    In ``add_memory``: when ``conversation`` is falsy after popping
    ``messages`` and ``text``, the tool returns the ``messages_missing`` error
    without making an HTTP call. No ``status`` key is present (this is a
    validation error, not an HTTP error).
    """
    tools, _config = tool_functions

    result = tools["add_memory"](ctx=_STUB_CTX)

    assert result["error"] == "messages_missing"
    assert isinstance(result["detail"], str)
    assert result["detail"]
    assert "status" not in result


def test_add_memory_with_malformed_messages_returns_invalid_messages(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``add_memory(messages=[{"role": "user"}])`` (missing ``content``) returns
    ``_error("invalid_messages", ...)``.

    In ``add_memory``: each message dict is validated as a ``ToolMessage``
    (the ``ToolMessage`` model requires both ``role`` and ``content``). A
    message missing ``content`` raises ``ValidationError``, caught and mapped
    to ``invalid_messages``. The detail string carries the validation error
    text (redacted/truncated by Pydantic's ``str(exc)``).
    """
    tools, _config = tool_functions

    result = tools["add_memory"](
        messages=[{"role": "user"}],  # missing "content"
        ctx=_STUB_CTX,
    )

    assert result["error"] == "invalid_messages"
    assert isinstance(result["detail"], str)
    assert result["detail"]
    assert "status" not in result


def test_update_memory_with_no_fields_returns_nothing_to_update(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``update_memory(memory_id="mem-1")`` with no ``text``/``metadata``/
    ``expiration_date`` returns ``_error("nothing_to_update", ...)``.

    In ``update_memory``: ``UpdateMemoryArgs`` with all-None fields
    serializes to an empty dict via ``model_dump(exclude_none=True)``, and the
    ``if not body`` guard returns ``nothing_to_update``. The memory_id is valid
    here (``"mem-1"``) so the ``invalid_memory_id`` path is not taken — this
    test isolates the no-fields guard.
    """
    tools, _config = tool_functions

    result = tools["update_memory"](memory_id="mem-1", ctx=_STUB_CTX)

    assert result["error"] == "nothing_to_update"
    assert isinstance(result["detail"], str)
    assert result["detail"]
    assert "status" not in result


def test_delete_entities_with_no_scope_returns_scope_missing(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``delete_entities()`` with no ``user_id``/``agent_id``/``run_id`` returns
    ``_error("scope_missing", ...)``.

    In ``delete_entities``: ``DeleteEntitiesArgs`` with all-None fields makes
    the ``next(...)`` over the (type, value) tuples return ``None``, so the
    ``scope is None`` guard fires. No HTTP call is made.
    """
    tools, _config = tool_functions

    result = tools["delete_entities"](ctx=_STUB_CTX)

    assert result["error"] == "scope_missing"
    assert isinstance(result["detail"], str)
    assert result["detail"]
    assert "status" not in result


@pytest.mark.parametrize(
    "tool_name",
    ["get_memory", "delete_memory", "get_memory_history"],
)
def test_memory_id_tools_reject_invalid_memory_id(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
    tool_name: str,
) -> None:
    """``get_memory`` / ``delete_memory`` / ``get_memory_history`` with an
    invalid ``memory_id`` return ``_error("invalid_memory_id", ...)``.

    Each of these tools wraps the ``Mem0OSSClient`` call in a ``try/except
    ValueError``. ``_validate_memory_id`` raises ``ValueError`` for a
    memory_id containing a slash (rejected by ``_MEMORY_ID_RE``), which is
    caught and mapped to ``invalid_memory_id``. Parametrized across the three
    tools so a regression in one try/except (e.g. a dropped ``except``) is
    isolated to a single failing case.
    """
    tools, _config = tool_functions

    result = tools[tool_name](memory_id=_INVALID_MEMORY_ID, ctx=_STUB_CTX)

    assert result["error"] == "invalid_memory_id"
    assert isinstance(result["detail"], str)
    assert result["detail"]
    assert "status" not in result


def test_update_memory_rejects_invalid_memory_id(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``update_memory`` with an invalid ``memory_id`` AND a valid field returns
    ``_error("invalid_memory_id", ...)``.

    In ``update_memory``: the ``nothing_to_update`` guard runs first (against
    ``body``), so to reach the ``_validate_memory_id`` path the test must pass
    a non-empty field (``text="updated"``). With a valid field present, the
    guard passes and ``_client(...).update(memory_id, body)`` calls
    ``_validate_memory_id``, which raises ``ValueError`` for the slash-bearing
    id; the ``except ValueError`` maps it to ``invalid_memory_id``.

    This is the ``update_memory``-specific companion to the parametrized test
    above — ``update_memory`` is excluded from that parametrization because its
    validation order (no-fields guard before memory_id validation) requires a
    different call shape (a valid field must be supplied).
    """
    tools, _config = tool_functions

    result = tools["update_memory"](
        memory_id=_INVALID_MEMORY_ID,
        text="updated",
        ctx=_STUB_CTX,
    )

    assert result["error"] == "invalid_memory_id"
    assert isinstance(result["detail"], str)
    assert result["detail"]
    assert "status" not in result


def test_delete_entities_with_invalid_scope_value_returns_invalid_entity(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
) -> None:
    """``delete_entities(user_id="bad/id")`` returns
    ``_error("invalid_entity", ...)``.

    Covers the ``except ValueError`` branch in ``delete_entities`` that is
    NOT exercised by the ``scope_missing`` test (task 8.3). With a valid scope
    present (``user_id`` is truthy), the ``scope is None`` guard passes and
    ``_client(...).delete_entity("user", "bad/id")`` is reached.
    ``delete_entity`` calls ``_validate_memory_id`` on both the entity type
    (``"user"`` — valid) and the entity id (``"bad/id"`` — rejected by
    ``_MEMORY_ID_RE`` for the slash); the ``ValueError`` propagates up to
    ``delete_entities``'s ``except ValueError`` and is mapped to
    ``invalid_entity``. No HTTP call reaches the wire (the validation raises
    before ``_call``), so no ``status`` key is present.
    """
    tools, _config = tool_functions

    result = tools["delete_entities"](user_id=_INVALID_MEMORY_ID, ctx=_STUB_CTX)

    assert result["error"] == "invalid_entity"
    assert isinstance(result["detail"], str)
    assert result["detail"]
    assert "status" not in result


# ---------------------------------------------------------------------------
# Task 8.4 — default user/agent injection (asserted via the received echo)
# ---------------------------------------------------------------------------
#
# ``server.py`` reads ``MEM0_DEFAULT_USER_ID`` / ``MEM0_DEFAULT_AGENT_ID`` into
# module-level constants (``ENV_DEFAULT_USER_ID``, ``ENV_DEFAULT_AGENT_ID``) at
# import time, so ``monkeypatch.setenv`` alone has no
# effect — the constants are patched directly via ``monkeypatch.setattr``
# (design.md decision 7). The ``tool_functions`` fixture patches only
# ``ENV_BASE_URL`` / ``ENV_API_KEY``; this test patches the two default-scope
# constants in addition, using distinct sentinel values so a leak of any other
# value (e.g. the host's real ``MEM0_DEFAULT_USER_ID``) is detectable.
#
# The assertion mechanism is the fake server's ``received`` echo (task 2.1):
# each handler records ``(method, path, query_params, json_body, headers)``.
# The test clears ``config.received`` before each tool call so the last entry
# is unambiguously the call under test, then asserts the default ``user_id``
# and ``agent_id`` reached the wire in the right payload location:
#
# - ``add_memory``: ``user_id`` and ``agent_id`` are top-level keys in the
#   POST /memories JSON body (``add_memory`` builds ``AddMemoryArgs`` with both,
#   then ``model_dump(exclude_none=True)`` keeps them).
# - ``get_memories``: ``user_id`` and ``agent_id`` are query params on the
#   GET /memories request (``get_memories`` builds ``GetMemoriesArgs`` with
#   both, serializes to params, passes to ``list_memories``).
# - ``search_memories``: ``user_id`` and ``agent_id`` are inside the
#   ``filters`` object in the POST /search JSON body
#   (``_with_default_filters`` injects both into the filters dict, then
#   ``SearchMemoriesArgs`` carries ``filters`` and
#   ``model_dump(exclude_none=True)`` keeps the nested dict).
#
# The ``add_memory`` user_id injection has a conditional:
# ``user_id=user_id if user_id else (default_user if not (agent_id or run_id)
# else None)``. With no caller ``agent_id``/``run_id`` and
# no caller ``user_id``, ``default_user`` is used — this is the case under
# test. (When the caller supplies ``agent_id`` or ``run_id`` but not
# ``user_id``, ``user_id`` stays ``None``; that path is not asserted here.)


_DEFAULT_USER_SENTINEL = "test-default-user-sentinel"
_DEFAULT_AGENT_SENTINEL = "test-default-agent-sentinel"


def test_default_user_and_agent_injected_into_add_memory_payload(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``add_memory(text="...")`` with no caller scope sends a POST /memories
    body containing both the default ``user_id`` and ``agent_id``.

    Patches ``ENV_DEFAULT_USER_ID`` and ``ENV_DEFAULT_AGENT_ID`` to distinct
    sentinels, calls ``add_memory(text="...")`` with no ``user_id``/
    ``agent_id``/``run_id``, and reads the fake server's ``received`` echo to
    assert both sentinels are top-level keys in the JSON body.
    """
    tools, config = tool_functions
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", _DEFAULT_USER_SENTINEL)
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", _DEFAULT_AGENT_SENTINEL)
    config.received.clear()

    tools["add_memory"](text="likes pizza", ctx=_STUB_CTX)

    assert len(config.received) == 1
    entry = config.received[0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/memories"
    json_body = entry["json_body"]
    assert json_body is not None
    assert json_body["user_id"] == _DEFAULT_USER_SENTINEL
    assert json_body["agent_id"] == _DEFAULT_AGENT_SENTINEL


def test_default_user_and_agent_injected_into_get_memories_params(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_memories()`` with no caller scope sends GET /memories query params
    containing both the default ``user_id`` and ``agent_id``.

    In ``get_memories``: ``GetMemoriesArgs(user_id=user_id or default_user,
    agent_id=agent_id or default_agent, ...)`` then
    ``model_dump(exclude_none=True)`` -> params dict. The fake server records
    query params as a flat ``{key: value}`` dict.
    """
    tools, config = tool_functions
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", _DEFAULT_USER_SENTINEL)
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", _DEFAULT_AGENT_SENTINEL)
    config.received.clear()

    tools["get_memories"](ctx=_STUB_CTX)

    assert len(config.received) == 1
    entry = config.received[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/memories"
    query_params = entry["query_params"]
    assert query_params["user_id"] == _DEFAULT_USER_SENTINEL
    assert query_params["agent_id"] == _DEFAULT_AGENT_SENTINEL


def test_default_user_and_agent_injected_into_search_memories_filters(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search_memories(query="...")`` with no caller filters sends a
    POST /search body whose ``filters`` object contains both the default
    ``user_id`` and ``agent_id``.

    In ``search_memories``: ``_with_default_filters(None, default_user,
    default_agent)`` -> ``{"user_id": default_user, "agent_id":
    default_agent}``; ``SearchMemoriesArgs`` carries that as ``filters``;
    ``model_dump(exclude_none=True)`` keeps the nested ``filters`` dict in the
    body. The fake server records the parsed JSON body, so ``filters`` is a
    nested dict in ``json_body``.
    """
    tools, config = tool_functions
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", _DEFAULT_USER_SENTINEL)
    monkeypatch.setattr(server, "ENV_DEFAULT_AGENT_ID", _DEFAULT_AGENT_SENTINEL)
    config.received.clear()

    tools["search_memories"](query="pizza", ctx=_STUB_CTX)

    assert len(config.received) == 1
    entry = config.received[0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/search"
    json_body = entry["json_body"]
    assert json_body is not None
    filters = json_body["filters"]
    assert filters["user_id"] == _DEFAULT_USER_SENTINEL
    assert filters["agent_id"] == _DEFAULT_AGENT_SENTINEL


def test_add_memory_with_agent_id_but_no_user_id_omits_user_id(
    tool_functions: tuple[dict[str, Any], FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``add_memory(text="...", agent_id="a1")`` with no caller ``user_id``
    sends a POST /memories body that contains ``agent_id`` but NOT ``user_id``.

    Covers the ``else None`` branch of ``add_memory``'s user_id resolution:
    ``user_id = user_id if user_id else (default_user if not (agent_id or
    run_id) else None)``. When the caller supplies ``agent_id`` (or ``run_id``)
    but not ``user_id``, ``user_id`` stays ``None``; ``AddMemoryArgs`` carries
    it as ``None`` and ``model_dump(exclude_none=True)`` drops it from the
    payload. This is the documented behavior the task-8.4 block comment calls
    out as a non-goal for the default-injection tests — asserted here so the
    branch is not silently untested.

    ``ENV_DEFAULT_USER_ID`` is patched to a sentinel so a leak of the default
    user into the payload is detectable (the assertion is ``"user_id" not in
    json_body``, which would fail if the ``else None`` branch regressed to
    ``else default_user``).
    """
    tools, config = tool_functions
    monkeypatch.setattr(server, "ENV_DEFAULT_USER_ID", _DEFAULT_USER_SENTINEL)
    config.received.clear()

    tools["add_memory"](text="likes pizza", agent_id="caller-agent", ctx=_STUB_CTX)

    assert len(config.received) == 1
    entry = config.received[0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/memories"
    json_body = entry["json_body"]
    assert json_body is not None
    assert json_body["agent_id"] == "caller-agent"
    assert "user_id" not in json_body
