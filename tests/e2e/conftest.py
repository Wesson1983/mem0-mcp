"""E2e fixtures for the mem0-mcp-server MCP transport (change: test-suite-foundation, task 10.1).

These fixtures drive the real Docker container at ``MCP_URL`` (default
``http://localhost:8765/mcp``) over the MCP Streamable HTTP transport. The
whole package is marked ``e2e`` so ``-m "not e2e"`` deselects it, and an
autouse fixture skips every test unless ``MEM0_E2E=1`` is set — the suite
must never fail in CI just because no container is running.

``parse_sse`` is the SSE-stream parser originally proven in the deleted
``verify_mcp.py`` manual handshake script (see change execution note M9):
it pulls the first JSON-RPC ``result``/``error`` object out of the ``data:``
lines of a Streamable HTTP response. Factored here as a shared helper rather
than reimplemented per test.

``mcp_session`` performs the JSON-RPC handshake
(``initialize`` → capture ``mcp-session-id`` → ``notifications/initialized``)
and yields an :class:`McpSession` helper exposing ``call(tool, arguments)``,
which posts a ``tools/call`` request and returns the parsed JSON-RPC object.

``test_scope`` mints unique ``user_id``/``agent_id`` values per test and
exposes a mutable dict for tests to hand memory IDs to later tests; in
``finally`` it calls ``delete_entities`` for the user scope, catching and
logging any exception so cleanup failure never masks a test failure.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

_log = logging.getLogger(__name__)

# MCP Streamable HTTP requires both content types in ``Accept`` for the server
# to pick the SSE response framing (per AGENTS.md "Verification" notes).
_MCP_ACCEPT = "application/json, text/event-stream"
_MCP_PROTOCOL_VERSION = "2025-06-18"
# Writes run LLM fact extraction + embeddings; a single add_memory can take
# tens of seconds on local models (AGENTS.md notes ~48s measured). 300s
# matches the server's own ``MEM0_HTTP_TIMEOUT`` default.
_MCP_CALL_TIMEOUT = 300


def _raise_on_http_error(resp: requests.Response) -> None:
    """Raise ``RuntimeError`` for HTTP >= 400 so transport failures surface.

    Without this, a 4xx/5xx response with a non-SSE body would make
    ``parse_sse`` return ``None`` and tests would fail with an opaque
    "no parseable JSON-RPC response" assertion instead of the underlying
    HTTP error. The 300s timeout already covers the no-response case; this
    covers the explicit-error case.
    """
    if resp.status_code >= 400:
        raise RuntimeError(
            f"MCP request to {resp.url} failed: HTTP {resp.status_code}; "
            f"body: {resp.text[:300]}"
        )


def parse_sse(text: str) -> dict[str, Any] | None:
    """Pull the first JSON-RPC ``result``/``error`` out of an SSE stream.

    Streamable HTTP responses are a sequence of ``data: <json>`` lines; the
    JSON-RPC envelope we want carries a ``result`` or ``error`` key. Lines
    that are not ``data:`` or that fail to parse as JSON are skipped. Returns
    ``None`` when no envelope is found so callers can distinguish "no
    response" from a real error payload. Lifted verbatim in spirit from the
    deleted ``verify_mcp.py`` ``parse_sse``.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if "result" in obj or "error" in obj:
            return obj  # type: ignore[no-any-return]
    return None


class McpSession:
    """Helper bound to one MCP session for posting JSON-RPC requests.

    Built by :func:`mcp_session` after the handshake. ``call`` posts a
    ``tools/call`` request and returns the parsed JSON-RPC object (the dict
    with ``result`` or ``error``); callers inspect ``result.content[0].text``
    for the tool's text payload, matching ``verify_mcp.py``'s pattern.
    """

    def __init__(
        self,
        url: str,
        base_headers: dict[str, str],
        http: requests.Session,
    ) -> None:
        self._url = url
        self._headers = base_headers
        self._id = 0
        self._http = http

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Post ``tools/call`` for ``tool`` with ``arguments``; return parsed JSON-RPC."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        resp = self._http.post(
            self._url, headers=self._headers, json=payload, timeout=_MCP_CALL_TIMEOUT
        )
        _raise_on_http_error(resp)
        return parse_sse(resp.text)

    def tools_list(self) -> dict[str, Any] | None:
        """Post ``tools/list``; return parsed JSON-RPC object."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
        }
        resp = self._http.post(
            self._url, headers=self._headers, json=payload, timeout=_MCP_CALL_TIMEOUT
        )
        _raise_on_http_error(resp)
        return parse_sse(resp.text)


def _mcp_url() -> str:
    return os.getenv("MCP_URL", "http://localhost:8765/mcp")


def _mcp_handshake(url: str, http: requests.Session) -> dict[str, str]:
    """Perform the MCP initialize / notifications/initialized handshake.

    Returns the session headers (base headers + ``MCP-Session-Id``) that
    subsequent requests must carry. Raises ``RuntimeError`` if the server
    does not return a session id. Factored out of the ``mcp_session`` fixture
    so module-scoped and function-scoped fixtures can share the same logic
    without duplicating the handshake steps.
    """
    base_headers = {
        "Accept": _MCP_ACCEPT,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
    }
    init_payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "e2e-test", "version": "0.1"},
        },
    }
    init_resp = http.post(url, headers=base_headers, json=init_payload, timeout=60)
    _raise_on_http_error(init_resp)
    session_headers = dict(base_headers)
    session_headers["MCP-Session-Id"] = init_resp.headers.get("mcp-session-id") or ""
    if not session_headers["MCP-Session-Id"]:
        raise RuntimeError(
            f"MCP initialize returned no mcp-session-id header "
            f"(HTTP {init_resp.status_code}); body: {init_resp.text[:300]}"
        )
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    notif_resp = http.post(url, headers=session_headers, json=notif, timeout=60)
    _raise_on_http_error(notif_resp)
    return session_headers


def tool_text(body: dict[str, Any] | None) -> str:
    """Extract the first content item's text from a ``tools/call`` result.

    The MCP ``tools/call`` result wraps the tool's return value as
    ``{"content": [{"type": "text", "text": "<json>"}], "isError": bool}``.
    Returns ``""`` when the shape is unexpected so callers can do substring
    checks without ``KeyError``/``IndexError`` masking the real assertion.
    Shared by ``test_mcp_transport.py`` and ``test_tools_call_round_trip.py``.
    """
    if not body:
        return ""
    result = body.get("result") or {}
    content = result.get("content") or []
    if not content or not isinstance(content, list):
        return ""
    first = content[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("text", ""))


def tool_result_json(body: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract and JSON-parse the tool payload from a ``tools/call`` response.

    Combines :func:`tool_text` with ``json.loads``; returns ``None`` when the
    text is absent or not valid JSON so callers can distinguish a missing
    response from a real error payload.
    """
    text = tool_text(body)
    if not text:
        return None
    try:
        parsed: dict[str, Any] = json.loads(text)
        return parsed
    except json.JSONDecodeError:
        return None


@pytest.fixture(scope="module")
def mcp_session() -> Iterator[McpSession]:
    """Perform the MCP handshake and yield an :class:`McpSession` helper.

    Module-scoped so all tests in a module share one session (one handshake
    instead of N). Tests in ``test_mcp_transport.py`` and
    ``test_tools_call_round_trip.py`` each get their own session because the
    fixture is module-scoped, not session-scoped.

    Steps (per AGENTS.md "Verification"):
      1. ``POST /mcp initialize`` → capture the ``mcp-session-id`` response
         header. ``Accept`` carries both content types so the server uses
         SSE framing.
      2. ``POST /mcp notifications/initialized`` with the session header
         (no response body expected).

    Yields a ``McpSession`` whose ``call``/``tools_list`` methods post
    subsequent requests with the captured session header. The HTTP session
    is closed on teardown.
    """
    url = _mcp_url()
    http = requests.Session()
    session_headers = _mcp_handshake(url, http)
    session = McpSession(url, session_headers, http)
    try:
        yield session
    finally:
        http.close()


@pytest.fixture(scope="module")
def test_scope(mcp_session: McpSession) -> Iterator[dict[str, str]]:
    """Mint a unique test scope and clean it up in ``finally``.

    Module-scoped so all tests in a module share one ``user_id``/``agent_id``
    pair. This lets ``test_add_memory`` store a ``memory_id`` that
    ``test_get_memory``/``test_get_memory_history``/``test_update_memory``/
    ``test_delete_memory`` read later via the shared mutable dict, expressing
    inter-test dependencies through the dict (plus skip-if-absent) rather
    than implicit file order (tasks 11.4/11.5/11.6/11.7).

    In ``finally`` it calls ``delete_entities`` for the ``user_id`` scope so
    orphaned test memories are removed; any cleanup exception is caught and
    logged so it never masks a test failure (per task 10.1).
    """
    scope: dict[str, str] = {
        "user_id": f"test_e2e_{uuid4().hex}",
        "agent_id": f"test_e2e_{uuid4().hex}",
    }
    try:
        yield scope
    finally:
        try:
            cleanup_resp = mcp_session.call(
                "delete_entities", {"user_id": scope["user_id"]}
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask a failure
            _log.warning(
                "e2e cleanup: delete_entities(user_id=%s) failed: %s",
                scope["user_id"],
                exc,
            )
        else:
            # Network success but JSON-RPC error response — log so a
            # server-side cleanup failure is observable, not silently dropped.
            if cleanup_resp and "error" in cleanup_resp:
                _log.warning(
                    "e2e cleanup: delete_entities(user_id=%s) returned error: %s",
                    scope["user_id"],
                    cleanup_resp["error"],
                )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test collected under ``tests/e2e/`` with ``e2e``.

    ``pytestmark = pytest.mark.e2e`` only works in test modules, not in
    conftest, so a package-level marker needs this hook instead. Without it
    ``-m "not e2e"`` would not deselect the e2e tests (verified: the
    module-level ``pytestmark`` in conftest is silently ignored). Applying
    the marker here lets CI run ``pytest -m "not e2e"`` and skip the whole
    package without the autouse skip fixture even firing.

    The path guard keeps the marker scoped to this package: pytest invokes
    conftest-level ``pytest_collection_modifyitems`` with the full item list
    (not just the conftest's subtree), so without the guard every test in
    the suite would be marked ``e2e`` and ``-m "not e2e"`` would deselect
    the entire suite.
    """
    e2e_marker = pytest.mark.e2e
    # When ``MEM0_E2E`` is not set, skip at collection time (before any
    # fixture setup) so module-scoped fixtures like ``mcp_session`` are not
    # instantiated — a function-scoped autouse skip would fire too late
    # (after module-scoped fixture setup) and the handshake would error
    # instead of skipping cleanly.
    skip_marker = pytest.mark.skip(
        reason="requires MEM0_E2E=1 plus a running container, mem0 OSS, and LM Studio"
    )
    e2e_enabled = os.getenv("MEM0_E2E") == "1"
    # Resolve this conftest's directory once; ``Path.is_relative_to`` handles
    # symlinks, case-insensitive filesystems, and relative-vs-absolute
    # resolution uniformly, replacing the earlier ``str(item.path)``
    # substring check (which could false-positive on a sibling dir whose
    # name contained ``tests/e2e`` and false-negative on a symlinked e2e).
    e2e_dir = Path(__file__).resolve().parent
    for item in items:
        # ``item.path`` is a ``pathlib.Path`` (or ``py.path.local`` on older
        # pytest); coerce to ``Path`` and resolve so the comparison is
        # canonical regardless of how pytest reported the path.
        if Path(str(item.path)).resolve().is_relative_to(e2e_dir):
            item.add_marker(e2e_marker)
            if not e2e_enabled:
                item.add_marker(skip_marker)
