"""E2e real-latency concurrency proof (change: test-suite-foundation, task 12.1).

This is the e2e counterpart of the integration concurrency test
(``tests/integration/test_concurrency.py::test_event_loop_yields_during_write_read_in_parallel``):
instead of a fake server with a configured 2.0s write latency, it uses the
real Docker container + mem0 OSS + LM Studio, where a single ``add_memory``
runs LLM fact extraction + embeddings and takes ~20-40s on local models
(AGENTS.md notes ~48s measured). The integration test proves the event loop
yields against fake latency; this test proves the same property against the
real deployment's actual latency — the thing the ``async-tool-execution``
refactor exists to fix.

The whole package is skipped unless ``MEM0_E2E=1`` (collection-time skip in
``tests/e2e/conftest.py::pytest_collection_modifyitems``).

Why two MCP sessions
--------------------

A single MCP session may serialize requests server-side (one in-flight
request per session is the Streamable HTTP default). Issuing the slow write
and the read on the *same* session would queue the read behind the write
regardless of the refactor, proving nothing. Two independent sessions (two
handshakes) let the read on session B dispatch concurrently with the write
on session A — the read completing in seconds while the write is still in
flight is the real-latency proof.

Why ``asyncio.to_thread``
-------------------------

``McpSession.call`` is a synchronous ``requests.Session.post`` (blocking I/O).
The test is ``async def`` so ``pytest-asyncio`` runs it on an event loop. To
issue the slow write and then the read without the write blocking the loop,
both calls are offloaded to worker threads via ``asyncio.to_thread``; the
``await asyncio.sleep(1)`` between them runs on the loop while the write
thread is blocked in the HTTP call. This mirrors how the refactored server
offloads sync tool bodies, and it is the only way to observe concurrency
from an async test driving a sync HTTP client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
import requests

from tests.e2e.conftest import (
    McpSession,
    _mcp_handshake,
    _mcp_url,
    tool_result_json,
)

_log = logging.getLogger(__name__)

# The read must complete in seconds, not tens of seconds. 5.0s is loose
# enough to absorb CI/container scheduler jitter and LM Studio warmup, yet
# tight enough to fail if the read is queued behind a 20-40s write (which
# would make read_elapsed ~ write_elapsed).
_READ_MAX = 5.0
# The write must actually be slow — a write under 10s would not prove the
# read overlapped with real blocking I/O (a fast write could finish before
# the read even starts). 10.0s is comfortably below the measured ~20-40s
# real-write latency, so a healthy stack passes with margin.
_WRITE_MIN = 10.0


@pytest.fixture(scope="module")
def concurrency_setup() -> Iterator[tuple[McpSession, McpSession, dict[str, str]]]:
    """Open two independent MCP sessions and mint a unique test scope.

    Yields ``(session_a, session_b, scope)`` where ``scope`` carries unique
    ``user_id``/``agent_id`` values. Teardown calls ``delete_entities`` for
    the user scope (via session A, catching/logging any failure so cleanup
    never masks a test failure — mirroring the ``test_scope`` fixture in
    ``conftest.py``) and closes both HTTP sessions.

    Module-scoped so the two handshakes happen once per module run, not per
    test. The scope is unique per module invocation via ``uuid4`` so parallel
    runs or repeated invocations do not collide.
    """
    url = _mcp_url()
    http_a = requests.Session()
    headers_a = _mcp_handshake(url, http_a)
    session_a = McpSession(url, headers_a, http_a)
    http_b = requests.Session()
    headers_b = _mcp_handshake(url, http_b)
    session_b = McpSession(url, headers_b, http_b)
    scope: dict[str, str] = {
        "user_id": f"test_e2e_{uuid4().hex}",
        "agent_id": f"test_e2e_{uuid4().hex}",
    }
    try:
        yield session_a, session_b, scope
    finally:
        try:
            session_a.call("delete_entities", {"user_id": scope["user_id"]})
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask a failure
            _log.warning(
                "e2e cleanup: delete_entities(user_id=%s) failed: %s",
                scope["user_id"],
                exc,
            )
        http_a.close()
        http_b.close()


async def test_read_completes_during_write(
    concurrency_setup: tuple[McpSession, McpSession, dict[str, str]],
) -> None:
    """A read on session B completes while a write on session A is still in flight.

    From session A, start ``tools/call add_memory`` (a real 20-40s write).
    ``await asyncio.sleep(1)`` to let the write enter its blocking I/O. From
    session B, call ``tools/call get_memories`` and measure elapsed time.

    Asserts (per task 12.1):
      - ``read_elapsed < 5.0`` — the read completes in seconds, not tens of
        seconds; if it were queued behind the write it would take ~write_elapsed.
      - ``write_elapsed > 10.0`` — proves the write was actually slow, so the
        read's fast completion is a real overlap, not a write that finished
        before the read started.
      - ``read_elapsed < write_elapsed / 2`` — the read did not wait for the
        write; a serialized read would have read_elapsed ~= write_elapsed.

    Both calls are offloaded to worker threads via ``asyncio.to_thread`` so
    the event loop stays free to run the ``asyncio.sleep(1)`` and dispatch
    the read while the write thread is blocked in HTTP I/O.
    """
    session_a, session_b, scope = concurrency_setup

    write_start = time.monotonic()
    # Offload the blocking write so the event loop can dispatch the read
    # concurrently. asyncio.to_thread returns a coroutine; awaiting it would
    # block the test until the write finishes, so we do NOT await here — we
    # keep the coroutine as a task and await it after the read.
    write_task = asyncio.create_task(
        asyncio.to_thread(
            session_a.call,
            "add_memory",
            {
                "text": "test e2e concurrency: real-latency write",
                "user_id": scope["user_id"],
                "agent_id": scope["agent_id"],
            },
        )
    )

    # Let the write enter its blocking I/O before issuing the read. 1s is
    # well within the 20-40s write window, so the write is still in flight
    # when the read starts — the read's fast completion is real overlap.
    await asyncio.sleep(1)

    read_start = time.monotonic()
    read_body = await asyncio.to_thread(
        session_b.call, "get_memories", {"user_id": scope["user_id"]}
    )
    read_elapsed = time.monotonic() - read_start

    # Await the write to completion and measure its total elapsed time.
    write_body = await write_task
    write_elapsed = time.monotonic() - write_start

    # The read must not have errored — a transport or auth failure on the
    # read would make the timing assertions meaningless (a fast error is
    # fast for the wrong reason). Surface the raw body before the timing
    # asserts so a failure here is diagnosable.
    assert read_body is not None, (
        "get_memories returned no parseable JSON-RPC response"
    )
    assert "error" not in read_body, (
        f"get_memories returned a JSON-RPC error: {read_body['error']}"
    )
    read_result = tool_result_json(read_body)
    assert read_result is not None, (
        f"get_memories returned no parseable tool payload: {read_body}"
    )
    assert "error" not in read_result, (
        f"get_memories tool error: {read_result.get('error')}"
    )

    # The write must also not have errored. A tool-level write error (e.g.
    # http_500 after the LLM extraction completed) would still occupy the
    # server for the write duration, so the concurrency timing proof would
    # hold — but the write would have failed, and the test must not pass on
    # a failed write. Check both the JSON-RPC envelope and the tool payload,
    # mirroring the read's checks above (review-12.1 [M1]).
    assert write_body is not None, (
        "add_memory returned no parseable JSON-RPC response"
    )
    assert "error" not in write_body, (
        f"add_memory returned a JSON-RPC error: {write_body['error']}"
    )
    write_result = tool_result_json(write_body)
    assert write_result is not None, (
        f"add_memory returned no parseable tool payload: {write_body}"
    )
    assert "error" not in write_result, (
        f"add_memory tool error: {write_result.get('error')}"
    )

    # The three timing assertions from task 12.1.
    assert read_elapsed < _READ_MAX, (
        f"read did not complete within {_READ_MAX}s "
        f"(read_elapsed={read_elapsed:.3f}s) — the read was likely queued "
        f"behind the write, which is the sync-dispatch failure mode the "
        f"async-tool-execution refactor exists to fix."
    )
    assert write_elapsed > _WRITE_MIN, (
        f"write was not slow enough to prove overlap "
        f"(write_elapsed={write_elapsed:.3f}s, threshold={_WRITE_MIN}s) — "
        f"the real write should take 20-40s on local models; a fast write "
        f"means the stack is not under real load and the test proves nothing."
    )
    assert read_elapsed < write_elapsed / 2, (
        f"read did not complete in less than half the write time "
        f"(read_elapsed={read_elapsed:.3f}s, write_elapsed={write_elapsed:.3f}s) "
        f"— a serialized read would have read_elapsed ~= write_elapsed; "
        f"this assertion failing means the read waited for the write."
    )
