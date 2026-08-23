# Reasoning: test-suite-foundation change

This file captures the full exploration reasoning that led to the
`test-suite-foundation` change proposal. It is not an OpenSpec planning
artifact; it is the thinking record.

---

## Starting point: the user's premise

> refactoring of the mcp server to an async approach requires strong
> regression test suite. It should include unit tests, integration tests,
> end-to-end tests for every endpoint or piece of functionality

The user opened `openspec/changes/async-tool-execution/tasks.md` and
asserted that the async refactor (already proposed as an OpenSpec change)
requires a test suite that doesn't exist yet.

## Evidence gathered

### OpenSpec state

- Two active changes: `async-tool-execution` (0/18 tasks, in-progress) and
  `batch-write-guardrails` (0/31 tasks, in-progress).
- No registered stores — work is repo-local.
- `async-tool-execution` has proposal + design + tasks complete; specs
  skipped (`skip_specs: true` — pure refactor, no behavior change).
- `batch-write-guardrails` explicitly declares `async-tool-execution` as a
  prerequisite (its `anyio.Lock`, `anyio.sleep` cooldown, and retry delay
  require `async def` tools).

### Test inventory (current state)

```
  Automated test suite        ❌  0 files (pytest is in dev deps, unused)
  CI pipeline                 ❌  no .github/, no .gitlab-ci, no azure-pipelines
  conftest.py / fixtures      ❌  none

  Manual verification scripts:
  ┌──────────────────────┐    7-step MCP handshake against a running
  │ verify_mcp.py        │    container + live mem0 OSS + LM Studio.
  │                      │    Covers: init, tools/list, list_entities,
  │                      │    add_memory, search_memories, get_memories
  │                      │    (4 of 10 tools). Not asserted. Not in CI.
  └──────────────────────┘
  ┌──────────────────────┐    perf_add_memory.py, perf_batch_add_memory.py
  │ perf_*.py            │    Timing measurement, not correctness.
  └──────────────────────┘
```

`pyproject.toml` declares `pytest>=8.3.4`, `ruff>=0.7.0`, `mypy>=1.18.2` in
`[dependency-groups].dev` — but none are used. No `tests/` directory, no
`test_*.py` files, no `conftest.py`.

The `async-tool-execution` tasks.md §3 "End-to-end verification" has 5
manual checkboxes (rebuild, add_memory, search_memories, list_entities,
concurrent read during write) — all unchecked, covers 4 of 10 tools, no
assertions.

### Server surface

`src/mem0_mcp_server/server.py` (720 lines) contains:
- 10 tool functions (all sync `def`): `add_memory`, `search_memories`,
  `get_memories`, `delete_all_memories`, `list_entities`, `get_memory`,
  `get_memory_history`, `update_memory`, `delete_memory`, `delete_entities`
- `Mem0OSSClient` with `_call` + 10 wrapper methods
- Pure helpers: `_validate_base_url`, `_redact`, `_validate_memory_id`,
  `_error`, `_int_env`, `_with_default_filters`, `_resolve_settings`
- `_client` cache + `clear_client_cache`
- `create_server()` factory (FastMCP + Smithery decorator)

### Transitive dependencies available

`mcp[cli]>=1.6.0,<2.0.0` pulls in: `anyio`, `httpx`, `httpx-sse`,
`jsonschema`, `pydantic`, `pydantic-settings`, `pyjwt`, `python-multipart`,
`pywin32`, `sse-starlette`, `starlette`, `typing-extensions`,
`typing-inspection`, `uvicorn`.

**Key finding**: `starlette` and `uvicorn` are already available as
transitive deps. A real-socket fake HTTP server for integration tests
requires zero new dependencies.

---

## Decision 1: Scope — Foundation suite (A) vs Minimal characterization (B)

### The three options

```
  (A) FOUNDATION SUITE — "characterize the whole server"
      ~25-40 test functions, lives forever, catches ANY future regression.
      Writing it: ~1-2 sessions. Against sync code first, then refactor.

  (B) MINIMAL CHARACTERIZATION — "just enough to make the refactor safe"
      ~6-10 test functions, scoped to refactor risk, expand-later.
      Writing it: ~half a session. Against sync code first, then refactor.

  (A′) FOUNDATION-MINIMAL — "characterize the refactor's blast radius,
       structured to grow"
      ~15-20 test functions. Closes (B)'s gaps. Structured so (A) is
      additive.
```

### Analysis: (B)'s gaps relative to the refactor's blast radius

The minimal suite's pitch is "smallest thing that makes the refactor safe."
But examining what it omits against what the refactor can break:

- **`_resolve_settings` env precedence** — refactor doesn't touch it. Safe
  to omit. (B) holds.
- **Schema validation / `exclude_none`** — refactor doesn't touch it. Safe
  to omit. (B) holds.
- **`_client` cache** — refactor doesn't touch cache logic, but concurrency
  test exercises cached-client-under-concurrency. (B) mostly holds.
- **Error paths per tool** — refactor doesn't touch these branches. Safe
  to omit. (B) holds.
- **Per-wrapper HTTP error mapping (4xx → `_error`, `RequestException` →
  cache clear)** — refactor touches `_call`'s `try/except` *position* (now
  wraps an `await`). Exception propagation is supposed to be identical, but
  "supposed to be" is what characterization tests are for. (B) only covers
  this via concurrency happy path, not per-status-code. **(B) has a gap.**
- **Timeout selection (write vs read)** — refactor preserves the logic, but
  it's now inside `functools.partial` binding. A misbind could silently drop
  `timeout`. (B) doesn't assert timeout reaches the fake server. **(B) has
  a gap.**

### Selection rationale

`batch-write-guardrails` is already scaffolded and explicitly depends on
`async-tool-execution`. The server will continue to be developed. That
pushes toward (A) or (A′), not (B).

The user chose **(A)** — the full foundation suite. The per-tool ×
per-error-state matrix is included now rather than deferred. The directory
structure supports additive growth for future changes
(`batch-write-guardrails` can extend without reorganizing).

---

## Decision 2: Fake server — Real-socket Starlette+uvicorn over mocks

### Why mocks cannot test the refactor's thesis

The refactor's defining behavior is "the event loop yields during HTTP I/O."
A mocked `requests.Session.request` returns in microseconds — there's
nothing for the event loop to yield for. A concurrency test using mocks
passes identically on sync code and async code:

```
  Mock approach:
    coroutine A: await client.add(body)     → mock returns instantly
    coroutine B: await client.list(...)     → mock returns instantly
    assert B completes "concurrently"       → PASSES on sync AND async
    (proves nothing — both are instant)

  Real-socket fake server:
    coroutine A: await client.add(body)     → real HTTP POST, server
                                               sleeps 2s in handler
    coroutine B: await client.list(...)     → real HTTP GET, server
                                               sleeps 50ms in handler
    assert B completes in <0.5s             → FAILS on sync (B blocks
                                               until A's 2s finishes)
                                              PASSES on async (loop
                                               yields during A's I/O)
```

### Options compared

| Option | What it is | New deps | Can test concurrency |
|---|---|---|---|
| 1. Starlette + uvicorn (real socket) | ~30-line ASGI app, handlers sleep | none (transitive) | ✓ |
| 2. httpx + ASGITransport (no socket) | Same app, hit via ASGITransport | none | ✗ (bypasses blocking socket) |
| 3. aiohttp test server | aiohttp.test_utils.TestServer | aiohttp (new) | ✓ |
| 4. pytest-httpserver | Plugin, real-socket server | pytest-httpserver (new) | ✓ |
| 5. http.server (stdlib) | stdlib server with sleep handlers | none | ✓ |

### Why option 2 (ASGITransport) is a trap

`httpx` + `ASGITransport` is the trendy "no socket, fast" option. It's
wrong for this refactor. `Mem0OSSClient` calls `requests.Session.request`,
which does a real blocking socket call. `ASGITransport` bypasses the socket
entirely — the request never leaves the event loop. So:

- The "event loop yields during I/O" test passes on sync code too (no
  blocking call to block with). Can't distinguish sync from async.
- The "N concurrent calls on the same `requests.Session`" test can't run —
  `requests` can't speak ASGITransport.

Option 2 tests the fake server's concurrency, not the client's. Rejected.

### Selection: Option 1

A Starlette app served by `uvicorn.Server` on a real `127.0.0.1` port, with
handlers that call `time.sleep(N)`:

- `requests.Session.request` does a real blocking socket call (so the event
  loop has something to yield for).
- The fake server can inject deterministic latency per route (writes slow,
  reads fast) — mirrors the real mem0 OSS profile.
- The same fake server serves the per-wrapper happy/4xx/5xx/
  `RequestException`/timeout tests — one fixture, whole integration layer.
- `requests.Session` thread-safety under concurrent calls is testable
  against a real socket.
- Zero new dependencies (Starlette + uvicorn already transitive via
  `mcp[cli]`).

### Async test runner

`pytest-asyncio` with `asyncio_mode = "auto"` over `anyio` pytest plugin.
The test coroutines are asyncio-shaped (`asyncio.create_task`,
`asyncio.gather`, `asyncio.sleep`). The server uses `anyio` internally, but
tests observe it from the outside via `requests` (sync HTTP client) and
`asyncio` concurrency primitives.

---

## Decision 3: E2e layer — Include, against real Docker stack

### The user's context

The user confirmed: "there is already full mem0 stack running in Docker
locally." This changes the value calculation — e2e can test against the
actual latency profile that motivated the refactor (20-40s real writes),
not a 2s fake.

### What e2e covers that integration can't

```
  INTEGRATION (fake server)          E2E (real stack)
  ─────────────────────────          ──────────────────
  Mem0OSSClient semantics     ✓      ✓
  Tool function semantics     ✓      ✓
  Event loop yields           ✓      ✓
  requests.Session thread-    ✓      ✓
    safety under concurrency
  ──────────────────────────────────────────────────────
  FastMCP dispatch             ✗      ✓  (iscoroutinefunction check,
  SSE / Streamable HTTP               │   async vs sync tool call path)
  transport                    ✗      ✓
  mcp-session-id handling      ✗      ✓
  create_server() factory      ✗      ✓
  X-API-Key reaches upstream   ✗      ✓
  Real LM Studio latency       ✗      ✓  (the 20-40s write that's the
  profile                              │   refactor's actual motivation)
  Container deployment path    ✗      ✓
```

### Target: real container, not in-process

E2e hits the running Docker container at `localhost:8765` (matching
`verify_mcp.py`). An in-process server bypasses the MCP transport (SSE
framing, session handling), which is exactly what the refactor touches at
the dispatch boundary. The precondition is "container rebuilt with
refactored code, mem0 OSS + LM Studio running."

### State pollution strategy

Dedicated test scopes (`user_id="test_e2e_<uuid>"`) with forgiving cleanup
in `finally` (log warnings on failure, don't fail the suite). Orphaned test
memories are scoped and harmless.

### Concurrency e2e test tolerance

```
  assert read_elapsed < 5.0           # read should be ~seconds
  assert write_elapsed > 10.0         # prove the write was actually slow
  assert read_elapsed < write_elapsed / 2  # read didn't wait for write
```

The `< 5.0` bound is generous because real LM Studio read latency varies
(0.05s cached, 2s with embedding compute). The key assertion is the ratio.

---

## Decision 4: CI — Bundle GitHub Actions workflow into this change

### The CI gap

A perfect pytest suite that nothing runs automatically is theater. There's
no `.github/`, no CI of any kind. "Automated regression suite" without
automation is a contradiction.

### Options

| Option | What it runs | Needs |
|---|---|---|
| (i) Bundle GitHub Actions | ruff + mypy + pytest unit + integration | GitHub repo (yes — pyproject points to github.com/mem0ai) |
| (ii) CI as separate change | same | a second change to scaffold |
| (iii) No CI, manual only | nothing | developer runs pytest by hand |

### Selection: (i)

The workflow is ~30 lines. The change is already in `pyproject.toml`. E2e
excluded by marker — CI doesn't need LM Studio. Without CI, the suite is
only as good as developer discipline — which is how this repo ended up with
`pytest` in dev deps and zero test files.

---

## Relationship to existing changes

### `async-tool-execution` (prerequisite)

This change must land before the async refactor. The tests are written
against current sync code first. The refactor's job becomes "make these
tests still pass." The key test (`test_event_loop_yields_during_write_read
_in_parallel`) is RED on sync code (the characterization proof that the
refactor is needed) and GREEN on async code (the proof that the refactor
worked).

### `batch-write-guardrails` (also benefits)

That change is more behavior-changing than the async refactor (adds lock,
cooldown, retry, batch limit) and also has no planned verification. The
suite grows at that point to cover the new error paths it introduces. The
directory structure supports additive growth.

---

## The one test that proves the refactor did anything

```
  test_event_loop_yields_during_write_read_in_parallel:

    coroutine A: await add_memory(...)        ──┐ 2s fake-server latency
    coroutine B: await get_memories(...)        │
                                                │
    assert B completes while A is in flight     │
    assert B wall-clock << A wall-clock         │
    (pre-refactor: B blocks until A finishes)   │
```

This test is the difference between "we think the refactor worked" and "we
know it did." It's RED on sync, GREEN on async. It's the only test that
observes the system boundary behavior the refactor changes. Everything else
is mechanics.

---

## Summary of decisions

| Decision | Choice | Key reason |
|---|---|---|
| Suite scope | (A) Foundation | Server will keep developing; `batch-write-guardrails` queued |
| Fake server | Real-socket Starlette+uvicorn | Mocks can't test concurrency thesis; zero new deps |
| Async test runner | pytest-asyncio, auto mode | Tests are asyncio-shaped; mainstream |
| E2e layer | Include, real Docker stack | Tests transport + real latency; stack already running |
| E2e target | Running container at :8765 | Tests full deployment path; matches verify_mcp.py |
| E2e cleanup | Forgiving finally | Don't mask test failures with cleanup failures |
| CI | Bundle GitHub Actions | "Automated" without automation is a lie |
| CI scope | ruff + mypy + pytest -m "not e2e" | E2e needs LM Studio; CI doesn't |
| Specs | skip_specs: true | Tooling change, no behavior change |

---

## Post-review corrections

A detailed review of the drafted artifacts surfaced the following, all folded
back into `tasks.md` / `design.md` / `proposal.md`:

1. **`monkeypatch.setenv` would not have worked anywhere.** `server.py`
   resolves `ENV_API_KEY`, `ENV_BASE_URL`, `ENV_DEFAULT_USER_ID`,
   `ENV_DEFAULT_AGENT_ID`, `_WRITE_TIMEOUT`, `_READ_TIMEOUT` into
   module-level constants at import time. Tests must patch the module
   attributes instead. This affected tasks 4.1, 4.3, 7.4, 8.1, 8.4 — the
   original wording would have produced tests that silently exercised the
   real defaults instead of the fake server. Now Decision 7 in design.md.

2. **The tool functions are not importable.** All 10 are closures defined
   inside `create_server()`, so "call `add_memory(...)` directly" needed a
   fixture that builds a server and extracts callables from the FastMCP tool
   registry. Task 8.1 now says so.

3. **A permanently-red test is worse than no test.** The concurrency proof
   ships as `xfail(strict=True)` so CI is green before the refactor and
   fails loudly (XPASS) the moment the refactor lands, forcing marker
   removal. Original plan had it simply failing, which normalizes red CI.
   Now Decision 6 in design.md.

4. **Timing bound loosened 0.5s → 1.0s.** Still a 2x margin under the 2.0s
   write latency and still decisively red on sync code (~2.05s), but no
   longer flake-prone on a loaded CI runner. Guidance added: if flakiness
   appears, raise `write_latency` rather than loosening the bound.

5. **The fake server needed a request echo + `received` recorder.** Three
   assertions were unimplementable without it — `functools.partial` kwarg
   survival, default user/agent injection, and concurrency cross-talk.
   Appends are lock-guarded because handlers run on uvicorn worker threads.

6. **Unbounded startup poll would hang the suite.** Now a 5s deadline that
   raises `RuntimeError`.

7. **`timeout` is not observable on the wire.** Split: task 7.4 asserts it
   via a delegating spy on `requests.Session.request`; task 7.5 asserts
   `method`/`url`/`params`/`json` via the server echo. Also caught that
   `delete_all` passes `timeout=_WRITE_TIMEOUT` explicitly while `DELETE`
   otherwise falls into the read branch — now explicitly asserted.

8. **Connection-failure test simplified.** Closed-port on `127.0.0.1`
   instead of trying to make Starlette drop a connection mid-response.

9. **`conftest_e2e.py` → `conftest.py`.** pytest only auto-discovers
   fixtures from files named `conftest.py`; the original name would have
   yielded no fixtures. Also added `pytestmark = pytest.mark.e2e` at package
   level so `-m "not e2e"` actually deselects the layer.

10. **E2e inter-test state made explicit.** Memory IDs pass through the
    `test_scope` fixture's mutable dict with skip-if-absent, rather than
    relying on file-order execution.

11. **Cache eviction test made cheap.** Patch `_CLIENT_CACHE_MAX` to 2
    instead of constructing 32 clients; clear the cache around every test.

12. **`openspec validate --change` is not a valid flag.** It is `--changes`
    (or `openspec validate <name> --type change`). Task 14.2 corrected.

13. **Scope description corrected to (A).** The design's risk section still
    described the suite as `(A′)`-shaped with a deferred error matrix,
    contradicting the chosen full-foundation scope. Non-goals rewritten to
    reflect what is actually excluded (log-output assertions, stdio/Smithery
    paths, production-code changes).

14. **Cross-change prerequisite is documentation only.** OpenSpec has no
    mechanical prerequisite field; task 14.3 reframed as adding notes to the
    two dependent proposals plus recording the xfail-marker removal in
    `async-tool-execution/tasks.md`.
