# Review — Task 12.1 (e2e real-latency concurrency proof)

Reviewer: python-pro + ponytail (full)
Date: 2026-08-23
Files reviewed: `tests/e2e/test_concurrent_read_during_write.py` (new, 214 lines),
`tests/e2e/conftest.py` (reuse), `tests/integration/test_concurrency.py` (cross-ref)

## Summary

pass-with-findings — The file implements exactly what Task 12.1 specifies:
two independent MCP sessions, a slow real write on session A, a read on
session B dispatched 1s later, and the three timing assertions
(`read_elapsed < 5.0`, `write_elapsed > 10.0`, `read_elapsed < write_elapsed / 2`).
The test collects cleanly, skips correctly without `MEM0_E2E=1` (verified:
`1 skipped`), `ruff check` is clean, `mypy` (strict) is clean on the file,
and the full non-e2e suite remains green (`211 passed, 13 deselected, 1 xfailed`).

Design decisions are sound and well-documented:
- **Two sessions** is the correct choice — a single Streamable HTTP session
  serializes requests server-side, so a same-session read would queue behind
  the write regardless of the refactor, proving nothing. Two handshakes let
  the read dispatch concurrently. This is documented in the module docstring.
- **`asyncio.to_thread`** is the correct offload mechanism for a sync
  `requests.Session.post` from an `async def` test — it frees the event loop
  for the `asyncio.sleep(1)` and the read dispatch while the write thread is
  blocked in HTTP I/O. This mirrors how the refactored server offloads sync
  tool bodies.
- **`asyncio.create_task(asyncio.to_thread(...))` for the write** (not
  awaited immediately) is correct — the write runs in a worker thread while
  the test coroutine proceeds to the sleep and the read. `await write_task`
  after the read reaps the write result and measures its full duration.
- **Fixture teardown** mirrors the `test_scope` fixture in `conftest.py`:
  `delete_entities` for the user scope in `finally`, catching/logging
  exceptions so cleanup never masks a test failure, plus closing both HTTP
  sessions. Module-scoped fixture = one pair of handshakes per module run.
- **Read-before-write assertion ordering** is correct: the read is asserted
  non-error before the timing assertions, so a fast read *error* (e.g. auth
  failure) does not pass the timing checks for the wrong reason.

Ponytail check: the file reuses `_mcp_handshake`, `McpSession`, `_mcp_url`,
and `tool_result_json` from `conftest.py` rather than reimplementing the
handshake/SSE parsing. No new abstraction, no scaffolding-for-later, no
one-implementation interface. The `_READ_MAX`/`_WRITE_MIN` constants are
named thresholds referenced multiple times — justified, not speculative.

One Medium finding (asymmetric error checking on the write) and two Low
findings (resource handling on exceptional paths, consistent with the
existing fixture pattern). Details below.

## Findings

### Critical

- None

### High

- None

### Medium

- **[M1] Write body is not checked for tool-level error — asymmetric with the
  read, and a tool-level write error would be silently ignored**
  - File: `tests/e2e/test_concurrent_read_during_write.py`, lines 189-194
  - What: The read is checked at both the JSON-RPC envelope level
    (`"error" not in read_body`) AND the tool-payload level
    (`tool_result_json(read_body)` + `"error" not in read_result`). The write
    is checked only at the JSON-RPC envelope level (`"error" not in write_body`).
    A write that returns a JSON-RPC *success* but a tool-level
    `{"error": "http_500", ...}` payload (e.g. the LLM extraction succeeded
    but the mem0 write failed downstream) would pass the write assertions
    silently.
  - Why it matters: the task spec assumes "a real 20-40s write" — i.e. a
    successful write. A tool-level write error after the LLM call still
    occupies the server for the write duration, so the *concurrency* proof
    (read fast during slow write) still holds — but the test would pass
    despite the write having failed, masking a real stack regression. The
    read's stricter checking proves this is the intended contract; the write
    should match.
  - Fix: add `tool_result_json(write_body)` + tool-error assertion for the
    write, mirroring the read. (Applied below.)

### Low

- **[L1] Setup-failure resource leak — if the second handshake raises,
  `http_a`/`session_a` are not closed**
  - File: `tests/e2e/test_concurrent_read_during_write.py`, lines 86-92
  - What: The two handshakes run before the `try/finally` that closes the
    HTTP sessions. If `_mcp_handshake(url, http_b)` raises, `http_a` is
    leaked.
  - Severity: Low — this matches the established `mcp_session` fixture
    pattern in `conftest.py` (handshake before `try`), so it is consistent
    with the codebase convention. A setup failure means the container is
    unreachable, in which case the first handshake already failed and
    nothing is leaked; the second-handshake-fails-after-first-succeeds case
    is implausible (same endpoint). Not fixing — adding a `try/except` around
    the second handshake would diverge from the existing fixture pattern for
    a near-impossible failure mode.

- **[L2] Orphaned `write_task` on read-failure — if the read raises, the
  write task is never awaited/cancelled**
  - File: `tests/e2e/test_concurrent_read_during_write.py`, lines 141-165
  - What: If `await asyncio.to_thread(session_b.call, ...)` raises (transport
    error on the read), the test coroutine unwinds before `await write_task`.
    The write worker thread keeps running until the 300s `_MCP_CALL_TIMEOUT`
    or the server responds, producing a "Task was destroyed but it is
    pending" warning and a lingering HTTP connection.
  - Severity: Low — e2e-only, failure is exceptional, and process/fixture
    teardown (`http_a.close()`) eventually reclaims the connection. A
    `try/finally` cancelling `write_task` on the exceptional path would be
    more robust but adds complexity to a single-test module. Not fixing —
    noted as a future improvement if this module gains more tests.

### Informational

- **[I1] Private-symbol imports (`_mcp_handshake`, `_mcp_url`) from conftest**
  - File: `tests/e2e/test_concurrent_read_during_write.py`, lines 51-56
  - What: The imports use underscore-prefixed names from `conftest.py`.
  - Assessment: acceptable — `conftest.py`'s docstring explicitly states
    `_mcp_handshake` was "Factored out of the `mcp_session` fixture so
    module-scoped and function-scoped fixtures can share the same logic
    without duplicating the handshake steps." Reuse is the intended use;
    reimplementing the handshake would violate ponytail's "already in this
    codebase? reuse it" rung.

## Mutation/contract audit

- **Two sessions vs one**: confirmed correct — the module docstring explains
  why, and the Streamable HTTP one-in-flight-per-session default makes a
  same-session test meaningless.
- **`asyncio.create_task` + `asyncio.to_thread`**: confirmed — `to_thread`
  returns a coroutine, `create_task` schedules it without awaiting, the
  write runs in a worker thread concurrently with the read. Verified the
  test collects and skips (cannot run without the full stack).
- **Timing measurement windows**: `write_start` before the write task is
  created, `write_elapsed` after `await write_task` — captures the full
  write duration. `read_start` before the read's `to_thread` await,
  `read_elapsed` after — captures the read's wall-clock duration including
  thread-pool dispatch. Both correct.
- **Cleanup**: `delete_entities` by `user_id` in fixture `finally` removes
  the test's memories; matches the `test_scope` fixture contract.
- **Skip gating**: the new file is under `tests/e2e/`, so
  `pytest_collection_modifyitems` applies the `e2e` marker and the
  collection-time skip when `MEM0_E2E != "1"`. Verified: `1 skipped`.
- **`asyncio_mode = "auto"`**: the `async def test_` is auto-treated as an
  asyncio test without a decorator. Confirmed by collection succeeding.

## Verification

- `python -m pytest tests/e2e/test_concurrent_read_during_write.py --collect-only -q`
  → `1 test collected`
- `python -m pytest tests/e2e/test_concurrent_read_during_write.py -q`
  → `1 skipped` (correct without `MEM0_E2E=1`)
- `python -m ruff check tests/e2e/test_concurrent_read_during_write.py`
  → `All checks passed!`
- `python -m mypy tests/e2e/test_concurrent_read_during_write.py`
  → `Success: no issues found in 1 source file`
- `python -m pytest -m "not e2e" -q`
  → `211 passed, 13 deselected, 1 xfailed` (no regressions)

Re-verified after [M1] fix: ruff clean, mypy clean, `1 skipped`, no
regressions.

Cannot verify the passing branch without `MEM0_E2E=1` + running container +
mem0 OSS + LM Studio. The timing assertions and error checks are correct by
construction; the real-latency proof is environment-dependent by design.
