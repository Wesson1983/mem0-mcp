"""Integration tests for concurrency — the ``async-tool-execution`` refactor's
thesis (change: test-suite-foundation, tasks 9.1 and 9.2).

These tests exercise ``Mem0OSSClient`` against the real-socket fake server
(``fake_mem0_server`` fixture from ``tests/conftest.py``) with non-zero
latency so that blocking I/O is observable on the event loop.

Two tests:

- **9.1** ``test_event_loop_yields_during_write_read_in_parallel`` — the
  characterization proof. A slow write (2.0s) is started as a background
  task, then a fast read (0.05s) is issued. On **sync** code the write
  blocks the event loop for its full duration, so the read cannot start
  until the write finishes — total elapsed ~2.05s, failing the
  ``read_elapsed < 1.0`` assertion. On **async** code (post-refactor) the
  write is offloaded to a worker thread via
  ``anyio.to_thread.run_sync``, the event loop yields, and the read
  completes concurrently in ~0.15s. The test is marked
  ``xfail(strict=True)`` while the server is still sync: the suite stays
  green (xfail), and the moment the refactor makes the test pass, the
  strict xfail flips to XPASS and turns the suite red — forcing the
  marker's removal as part of ``async-tool-execution``.

- **9.2** ``test_concurrent_calls_same_cached_client`` — 10 concurrent
  reads through the same ``Mem0OSSClient`` (same ``requests.Session``)
  via ``asyncio.gather``. Each call sends a distinct ``user_id`` query
  param; the fake server echoes the received request back in the response
  body (``_received.query_params``), so the test asserts each response
  carries its own ``user_id`` — catching response cross-talk between
  concurrent calls sharing one session. Passes on both sync and async
  code (concurrent reads on the same ``requests.Session`` are low-risk
  per the design).

Sync/async compatibility layer
-------------------------------

``Mem0OSSClient`` wrappers are ``def`` (sync) today and will become
``async def`` after ``async-tool-execution``. The tests must run
unchanged against both. ``_await_or_call`` calls the method and inspects
the result: if it is a coroutine (async code), it is awaited; if it is a
plain value (sync code), it is returned directly. On sync code the
blocking call runs inline on the event loop thread — which is exactly
the behavior that makes the 9.1 assertion fail (the loop is frozen for
the write's duration). On async code the ``await`` offloads to a worker
thread and the loop yields.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Iterator
from typing import Any, TypeVar, cast

import pytest

from mem0_mcp_server.server import Mem0OSSClient, clear_client_cache
from tests.conftest import FakeMem0Config

_T = TypeVar("_T")

_API_KEY = "test-concurrency-api-key-aaaaaaaa"


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterator[None]:
    """Clear ``_CLIENT_CACHE`` before and after every test in this module.

    Mirrors the isolation fixture in ``test_mem0_oss_client.py`` and
    ``test_tool_functions.py``: ``_CLIENT_CACHE`` is process-global mutable
    state, and a stale entry bound to a previous test's torn-down fake server
    would produce ``http_request_failed`` errors. Clearing before *and* after
    each test keeps each test deterministic regardless of pass/fail.
    """
    clear_client_cache()
    yield
    clear_client_cache()


async def _await_or_call(
    method: Callable[..., _T], *args: Any, **kwargs: Any
) -> _T:
    """Call ``method`` and await its result if it is a coroutine.

    On sync code (pre-refactor): ``method(*args, **kwargs)`` executes
    inline and returns a plain value. The blocking I/O runs on the
    caller's thread — for a background ``asyncio.Task``, that is the
    event loop thread, which freezes the loop for the call's duration.
    This is the behavior 9.1 characterizes as broken.

    On async code (post-refactor): ``method(*args, **kwargs)`` returns a
    coroutine without executing. ``inspect.isawaitable`` detects it and
    ``await`` drives the coroutine, which internally offloads
    ``requests.Session.request`` to a worker thread via
    ``anyio.to_thread.run_sync`` and yields the event loop.

    This wrapper is the single point that lets the concurrency tests run
    unchanged across the sync→async refactor. It does NOT use
    ``asyncio.to_thread`` to wrap sync calls — doing so would make 9.1
    pass on sync code (the write would run in a thread, freeing the
    loop), defeating the characterization proof.
    """
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        # ``Callable[..., _T]`` does not capture that async methods return
        # ``Coroutine[...]``; ``inspect.isawaitable`` is a runtime check
        # mypy cannot narrow on. The cast is the documented escape hatch.
        return cast("_T", await result)
    return result


# ---------------------------------------------------------------------------
# Task 9.1 — event loop yields during write/read in parallel (xfail on sync)
# ---------------------------------------------------------------------------
#
# The test starts a slow write (write_latency=2.0) as a background
# ``asyncio.Task``, waits 0.1s for the write to enter its blocking I/O, then
# issues a fast read (read_latency=0.05) and measures the total elapsed time
# from the start of the wait through the read's completion.
#
# Why the measurement window includes the ``asyncio.sleep(0.1)``:
# On sync code, ``_await_or_call`` calls ``client.add`` inline, which blocks
# the event loop thread for the full 2.0s write latency. The
# ``await asyncio.sleep(0.1)`` that yielded control to the loop cannot
# resume until the write finishes — so the sleep actually takes ~2.0s, not
# 0.1s. The read then takes ~0.05s. Total ``read_elapsed`` ~2.05s, failing
# the ``< 1.0`` assertion. On async code, the write is offloaded to a
# thread; the sleep resumes after 0.1s; the read offloads and completes in
# ~0.05s. Total ``read_elapsed`` ~0.15s, passing.
#
# ``xfail(strict=True)``: the test is RED on sync code by design — it is the
# characterization proof that the ``async-tool-execution`` refactor is
# needed. ``strict=True`` means that once the refactor makes the test pass
# (XPASS), the marker itself fails the suite, forcing its removal. The
# comment on the marker states this requirement so the refactor's author
# knows to remove it.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED on sync code: the write blocks the event loop for ~2.0s so the "
        "read cannot start until the write finishes (read_elapsed ~2.05s, "
        "failing the < 1.0 assertion). GREEN on async code: the write is "
        "offloaded to a worker thread and the read completes concurrently "
        "(~0.15s). This xfail(strict=True) marker MUST be removed as part of "
        "the async-tool-execution change — strict=True turns an XPASS into a "
        "suite failure, forcing the marker's removal when the refactor lands."
    ),
)
async def test_event_loop_yields_during_write_read_in_parallel(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """A read issued during a slow write completes without waiting for it.

    Configures the fake server with ``write_latency=2.0`` and
    ``read_latency=0.05``. Starts ``client.add(...)`` as a background
    task, sleeps 0.1s to let the write enter its blocking I/O, then calls
    ``client.list_memories(...)`` and measures the total elapsed time.

    On sync code the write blocks the event loop; the sleep and the read
    cannot proceed until the write finishes (~2.05s total). On async code
    the write is offloaded to a worker thread and the read completes
    concurrently (~0.15s). The ``read_elapsed < 1.0`` assertion
    distinguishes the two — it is the refactor's characterization proof.
    """
    base_url, config = fake_mem0_server
    config.write_latency = 2.0
    config.read_latency = 0.05
    client = Mem0OSSClient(base_url, _API_KEY)

    write_body = {
        "messages": [{"role": "user", "content": "likes pizza"}],
        "user_id": "u",
    }

    # Start the write as a background task. On sync code, _await_or_call
    # calls client.add inline — when the event loop runs this task, the
    # blocking call freezes the loop for write_latency. On async code,
    # _await_or_call awaits the coroutine, which offloads to a thread.
    write_task = asyncio.create_task(_await_or_call(client.add, write_body))

    # Measure from the start of the wait window through the read's
    # completion. On sync code the sleep blocks for the write's full
    # duration (the loop is frozen), so this captures the read being
    # blocked behind the write. On async code the sleep takes 0.1s and
    # the read takes ~0.05s.
    read_elapsed_start = time.monotonic()
    await asyncio.sleep(0.1)  # let the write enter its blocking I/O
    read_result = await _await_or_call(
        client.list_memories, {"user_id": "u"}
    )
    read_elapsed = time.monotonic() - read_elapsed_start

    assert read_elapsed < 1.0, (
        f"read did not complete concurrently with the write "
        f"(read_elapsed={read_elapsed:.3f}s, write_latency=2.0s) — "
        f"the event loop was blocked by the write. "
        f"This is the sync-code characterization failure; the "
        f"async-tool-execution refactor must make this pass."
    )

    # Await the write and assert it returned the canned write response.
    # On sync code this line is unreachable (the assertion above fails,
    # which is the xfail). On async code the write completes in the
    # background and its result is the canned POST /memories body.
    write_result = await write_task
    assert "error" not in write_result
    assert write_result["results"] == [
        {"id": "mem-1", "memory": "likes pizza", "event": "ADD"}
    ]
    # Confirm the read also returned valid data (not corrupted by the
    # concurrent write).
    assert "error" not in read_result


# ---------------------------------------------------------------------------
# Task 9.2 — concurrent calls on the same cached client (no cross-talk)
# ---------------------------------------------------------------------------
#
# 10 concurrent ``client.list_memories`` calls through the same
# ``Mem0OSSClient`` instance (same ``requests.Session``) via
# ``asyncio.gather``. Each call sends a distinct ``user_id`` query param.
# The fake server's handler records the received request (including
# ``query_params``) and echoes it back in the response body under the
# ``_received`` key (task 2.1 echo contract). The test asserts each
# response carries its own ``user_id`` — catching cross-talk between
# concurrent calls sharing one session, which a "no exceptions" assertion
# alone would miss.
#
# This test passes on both sync and async code. On sync code,
# ``_await_or_call`` calls ``client.list_memories`` inline; the 10
# coroutines scheduled by ``asyncio.gather`` run sequentially on the
# event loop thread (each blocking for read_latency), but each returns
# its own response — no cross-talk. On async code, the 10 calls are
# offloaded to worker threads and run concurrently; ``requests.Session``
# handles concurrent reads safely (low-risk per design.md). Either way,
# each response echoes its own ``user_id``.


_N_CONCURRENT_READS = 10


async def test_concurrent_calls_same_cached_client(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """10 concurrent reads on the same ``requests.Session`` produce no
    response cross-talk.

    Fires ``N`` concurrent ``client.list_memories({"user_id": f"u{i}"})``
    calls through a single ``Mem0OSSClient`` instance via
    ``asyncio.gather``. The fake server echoes the received request back
    in each response body (``_received.query_params.user_id``), so the
    test asserts ``results[i]["_received"]["query_params"]["user_id"]``
    matches ``f"u{i}"`` — if concurrent calls sharing one session
    cross-talked (response ``i`` carrying call ``j``'s ``user_id``), this
    assertion would fail.
    """
    base_url, config = fake_mem0_server
    # A small read latency ensures the 10 calls actually overlap on async
    # code (each handler sleeps 0.05s in a threadpool worker, so all 10 are
    # in-flight simultaneously). Without it, zero-latency calls might
    # serialize even in separate threads, weakening the cross-talk guard.
    # On sync code the calls run sequentially (10 * 0.05s = 0.5s), which is
    # fine — the test still validates per-response correctness.
    config.read_latency = 0.05
    client = Mem0OSSClient(base_url, _API_KEY)

    coros = [
        _await_or_call(client.list_memories, {"user_id": f"u{i}"})
        for i in range(_N_CONCURRENT_READS)
    ]
    results = await asyncio.gather(*coros)

    assert len(results) == _N_CONCURRENT_READS

    for i, result in enumerate(results):
        # No call produced an error dict.
        assert "error" not in result, (
            f"concurrent read {i} returned an error: {result}"
        )
        # The fake server merges ``_received`` into dict bodies (task 2.1
        # echo contract). Each response must echo its own ``user_id`` —
        # not another call's. This is the cross-talk guard.
        received = result["_received"]
        assert received["query_params"]["user_id"] == f"u{i}", (
            f"concurrent read {i} echoed user_id="
            f"{received['query_params']['user_id']!r}, expected 'u{i}' "
            f"— response cross-talk between concurrent calls sharing one "
            f"requests.Session"
        )
