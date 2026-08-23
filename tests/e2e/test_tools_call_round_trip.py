"""E2e tool round-trip tests over the MCP transport (change: test-suite-foundation, tasks 11.1-11.10).

Each test calls a ``tools/call`` against the real Docker container via the
``mcp_session`` fixture and asserts a non-error response. The tests share a
module-scoped ``test_scope`` (one ``user_id``/``agent_id`` pair) so that
``test_add_memory`` can store a ``memory_id`` in the scope dict for
``test_get_memory`` / ``test_get_memory_history`` / ``test_update_memory`` /
``test_delete_memory`` to read — dependencies are expressed through the
shared dict plus skip-if-absent, not implicit file order (tasks 11.4/11.7).

The whole package is skipped unless ``MEM0_E2E=1`` (collection-time skip in
``tests/e2e/conftest.py::pytest_collection_modifyitems``).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.conftest import McpSession, tool_result_json


def _assert_no_error(body: dict[str, Any] | None, tool: str) -> dict[str, Any]:
    """Assert no JSON-RPC or tool-level error; return the parsed tool payload.

    Extracts and JSON-parses the tool payload from the ``tools/call`` response,
    checking both the JSON-RPC envelope (``body["error"]``) and the tool's own
    return value (``result["error"]``). Fails with a clear message naming the
    tool and the raw body so a transport or tool-level error is immediately
    diagnosable.
    """
    assert body is not None, f"{tool}: no parseable JSON-RPC response"
    assert "error" not in body, f"{tool}: JSON-RPC error: {body['error']}"
    result = tool_result_json(body)
    assert result is not None, f"{tool}: no parseable tool payload in response: {body}"
    assert "error" not in result, f"{tool}: tool error: {result.get('error')}"
    return result


def _extract_memory_id(result: dict[str, Any]) -> str | None:
    """Pull the memory ID from an ``add_memory`` response.

    mem0 OSS ``POST /memories`` returns ``{"results": [{"id": "...", ...}]}``;
    the ID is at ``results[0]["id"]``. Returns ``None`` if the shape is
    unexpected so callers can assert with a diagnostic message.
    """
    results = result.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    mem_id = first.get("id")
    return str(mem_id) if mem_id else None


# ---------------------------------------------------------------------------
# Task 11.1 — add_memory
# ---------------------------------------------------------------------------


def test_add_memory(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call add_memory`` returns a non-error response with a memory ID.

    Stores the memory under the test scope's ``user_id`` + ``agent_id`` and
    saves the returned ``memory_id`` into the shared ``test_scope`` dict for
    downstream tests (11.4-11.7) to read.
    """
    body = mcp_session.call(
        "add_memory",
        {
            "text": "test e2e memory",
            "user_id": test_scope["user_id"],
            "agent_id": test_scope["agent_id"],
        },
    )
    result = _assert_no_error(body, "add_memory")
    memory_id = _extract_memory_id(result)
    assert memory_id, f"add_memory response did not contain a memory ID: {result}"
    test_scope["memory_id"] = memory_id


# ---------------------------------------------------------------------------
# Task 11.2 — search_memories
# ---------------------------------------------------------------------------


def test_search_memories(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call search_memories`` returns a non-error response with results."""
    body = mcp_session.call(
        "search_memories",
        {"query": "test", "filters": {"user_id": test_scope["user_id"]}},
    )
    result = _assert_no_error(body, "search_memories")
    assert "results" in result, f"search_memories response missing 'results': {result}"


# ---------------------------------------------------------------------------
# Task 11.3 — get_memories
# ---------------------------------------------------------------------------


def test_get_memories(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call get_memories`` returns a non-error response."""
    body = mcp_session.call(
        "get_memories",
        {"user_id": test_scope["user_id"]},
    )
    _assert_no_error(body, "get_memories")


# ---------------------------------------------------------------------------
# Task 11.4 — get_memory (depends on 11.1's memory_id)
# ---------------------------------------------------------------------------


def test_get_memory(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call get_memory`` with the ID from ``test_add_memory``.

    Skips with a clear reason if the ID is absent (``test_add_memory`` failed
    or was deselected) rather than failing on a ``KeyError`` (task 11.4).
    """
    memory_id = test_scope.get("memory_id")
    if not memory_id:
        pytest.skip(
            "test_get_memory requires a memory_id from test_add_memory, "
            "which did not run or did not produce one"
        )
    body = mcp_session.call("get_memory", {"memory_id": memory_id})
    _assert_no_error(body, "get_memory")


# ---------------------------------------------------------------------------
# Task 11.5 — get_memory_history (depends on 11.1's memory_id)
# ---------------------------------------------------------------------------


def test_get_memory_history(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call get_memory_history`` with the ID from ``test_add_memory``."""
    memory_id = test_scope.get("memory_id")
    if not memory_id:
        pytest.skip(
            "test_get_memory_history requires a memory_id from test_add_memory, "
            "which did not run or did not produce one"
        )
    body = mcp_session.call("get_memory_history", {"memory_id": memory_id})
    _assert_no_error(body, "get_memory_history")


# ---------------------------------------------------------------------------
# Task 11.6 — update_memory (depends on 11.1's memory_id)
# ---------------------------------------------------------------------------


def test_update_memory(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call update_memory`` with the ID from ``test_add_memory`` and new text."""
    memory_id = test_scope.get("memory_id")
    if not memory_id:
        pytest.skip(
            "test_update_memory requires a memory_id from test_add_memory, "
            "which did not run or did not produce one"
        )
    body = mcp_session.call(
        "update_memory",
        {"memory_id": memory_id, "text": "test e2e memory (updated)"},
    )
    _assert_no_error(body, "update_memory")


# ---------------------------------------------------------------------------
# Task 11.7 — delete_memory (depends on 11.1's memory_id; ordered after 11.5/11.6)
# ---------------------------------------------------------------------------


def test_delete_memory(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call delete_memory`` with the ID from ``test_add_memory``.

    Ordered after ``test_get_memory_history`` and ``test_update_memory`` (which
    need the memory to still exist) via the shared ``test_scope`` dict plus
    skip-if-absent, not implicit file order (task 11.7). After deleting, the
    ``memory_id`` is removed from the dict so any later test that needs the
    memory to exist would skip instead of operating on a deleted resource.
    """
    memory_id = test_scope.get("memory_id")
    if not memory_id:
        pytest.skip(
            "test_delete_memory requires a memory_id from test_add_memory, "
            "which did not run or did not produce one"
        )
    body = mcp_session.call("delete_memory", {"memory_id": memory_id})
    _assert_no_error(body, "delete_memory")
    test_scope.pop("memory_id", None)


# ---------------------------------------------------------------------------
# Task 11.8 — delete_all_memories
# ---------------------------------------------------------------------------


def test_delete_all_memories(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call delete_all_memories`` with the test scope."""
    body = mcp_session.call(
        "delete_all_memories",
        {"user_id": test_scope["user_id"]},
    )
    _assert_no_error(body, "delete_all_memories")


# ---------------------------------------------------------------------------
# Task 11.9 — list_entities
# ---------------------------------------------------------------------------


def test_list_entities(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call list_entities`` returns a non-error response containing the test scope.

    After ``delete_all_memories`` (11.8) the entity record itself still exists
    (delete-all clears memories but keeps the entity), so the scope's
    ``user_id`` should appear in the entity listing. The exact response shape
    of mem0 OSS ``GET /entities`` is not pinned here — we check the user_id
    appears in the stringified result, which is robust against shape variation.

    Note: a substring check on ``str(result)`` could in theory false-positive
    if the UUID suffix appeared inside another entity's ID. The ``test_e2e_``
    prefix plus 32-char hex from ``uuid4().hex`` makes collision negligible;
    pinning the response shape would be a future improvement once the OSS
    ``/entities`` schema is documented.
    """
    body = mcp_session.call("list_entities")
    result = _assert_no_error(body, "list_entities")
    result_str = str(result)
    assert test_scope["user_id"] in result_str, (
        f"list_entities response does not contain the test scope's user_id "
        f"({test_scope['user_id']}): {result_str[:500]}"
    )


# ---------------------------------------------------------------------------
# Task 11.10 — delete_entities
# ---------------------------------------------------------------------------


def test_delete_entities(mcp_session: McpSession, test_scope: dict[str, str]) -> None:
    """``tools/call delete_entities`` with the test scope's ``user_id``."""
    body = mcp_session.call(
        "delete_entities",
        {"user_id": test_scope["user_id"]},
    )
    _assert_no_error(body, "delete_entities")
