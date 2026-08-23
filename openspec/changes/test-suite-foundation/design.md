## Context

The MCP server (`src/mem0_mcp_server/server.py`) has 10 tool functions, all
sync, all calling `requests.Session.request` via `Mem0OSSClient._call`. There
are zero automated tests — `pytest>=8.3.4` is declared in dev deps but unused.
No `conftest.py`, no `tests/` directory, no CI. The only verification is
`verify_mcp.py` (manual 7-step JSON-RPC handshake, 4 of 10 tools, no
assertions) and two `perf_*.py` timing scripts.

`mcp[cli]>=1.6.0,<2.0.0` already pulls in `starlette`, `uvicorn`, `httpx`,
`anyio`, and `sse-starlette` as transitive dependencies. This means a
real-socket fake HTTP server for integration tests requires zero new
dependencies.

See proposal.md for motivation and the prerequisite relationship to
`async-tool-execution` and `batch-write-guardrails`.

## Architecture

### Test layer separation

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER        │  WHAT IT PROVES              │  DEPENDENCIES        │
├───────────────┼──────────────────────────────┼──────────────────────┤
│  unit         │  pure helper correctness     │  none (in-process)   │
│               │  env precedence logic        │                      │
│               │  cache behavior              │                      │
│               │  schema validation           │                      │
├───────────────┼──────────────────────────────┼──────────────────────┤
│  integration  │  Mem0OSSClient HTTP seman    │  fake HTTP server    │
│               │  tool function semantics     │  (Starlette+uvicorn  │
│               │  ★ event loop yields         │   on real socket)    │
│               │  ★ N-concurrent-same-client  │                      │
├───────────────┼──────────────────────────────┼──────────────────────┤
│  e2e          │  MCP transport round-trip    │  Docker container +  │
│               │  FastMCP async dispatch      │  mem0 OSS +          │
│               │  SSE / session handling      │  LM Studio           │
│               │  ★ real-latency concurrency  │  (gated MEM0_E2E=1)  │
│               │  auth against real server    │                      │
└───────────────┴──────────────────────────────┴──────────────────────┘
```

The layers are complementary, not redundant. Integration runs every push
(fast, deterministic, proves mechanics). E2e runs when explicitly invoked
(slow, real latency, proves the thesis against the actual problem).

### Why mocks cannot test the refactor's thesis

The `async-tool-execution` refactor's defining behavior is "the event loop
yields during HTTP I/O so concurrent operations don't block each other." A
mocked `requests.Session.request` returns in microseconds — there's nothing
for the event loop to yield for. A concurrency test using mocks passes
identically on sync code and async code, proving nothing about the refactor.

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
    assert B completes in <1.0s             → FAILS on sync (B blocks
                                               until A's 2s finishes)
                                              PASSES on async (loop
                                               yields during A's I/O)
```

### Fake server design

```
  Test process                  uvicorn (background thread)       Mem0OSSClient
  ────────────                  ────────────────────────          ──────────────
  start uvicorn          ─────► Starlette app listening
  on 127.0.0.1:0                on assigned port
                                │
  await client.add(body)         │
       │                        │
       ▼                        │
  anyio.to_thread.run_sync       │  (after async refactor;
       │                        │   before refactor, direct call)
       ▼                        │
  requests.Session.post  ─────► │  POST /memories
  (blocks this thread)          │      │
                                │      ▼
                                │  handler: time.sleep(write_latency)
                                │  return canned JSON
                                │      │
                                │      ▼
  ◄──── response ───────────── ─┤  HTTP 200
       │
       ▼
  await resumes
```

The fake server is a Starlette app with routes matching the mem0 OSS REST
surface (`/memories`, `/search`, `/entities`, `/memories/{id}`,
`/memories/{id}/history`, `/entities/{type}/{id}`). Each handler:

1. Reads the mutable `responses` dict to determine status code and response
   body for this route (defaults to canned happy-path JSON).
2. Sleeps for the configured latency (`write_latency` for POST/PUT/PATCH/DELETE,
   `read_latency` for GET) to simulate real I/O blocking.
3. Records the received request (method, path, query params, JSON body,
   auth/content-type headers) into a `received` list guarded by a
   `threading.Lock`, and echoes those details back in the response body.
4. Returns the canned response.

The fixture binds to port `0` (OS-assigned), reads back the actual port, and
yields `base_url` + the mutable config (`responses`, latency settings,
`received`) so tests can override status codes per-test and assert on what
actually reached the wire.

The request echo is what makes three otherwise-untestable things assertable:
`functools.partial` kwarg survival (did `params`/`json` reach the wire?),
default user/agent injection (did `agent_id` get added to the payload?), and
response cross-talk under concurrency (did call `i` get call `i`'s response?).
Startup polling is bounded by a 5s deadline that raises rather than hanging.

### E2e architecture

E2e tests hit the real Docker container at `localhost:8765` (matching
`verify_mcp.py`'s pattern). The precondition is "container rebuilt with
refactored code, mem0 OSS + LM Studio running." This is documented in the
skip message when `MEM0_E2E != "1"`.

```
  E2e test flow:
    1. Skip if MEM0_E2E != "1"
    2. MCP handshake: POST /mcp initialize → capture mcp-session-id
    3. POST /mcp notifications/initialized
    4. POST /mcp tools/list → assert 10 tools
    5. For each tool: POST /mcp tools/call → assert non-error response
    6. Concurrency test: start add_memory (20-40s), fire get_memories
       from 2nd session, assert read completes < 5s
    7. Cleanup: delete_entities for test scope (in finally, log on failure)
```

Test state is isolated via dedicated scopes
(`user_id="test_e2e_<uuid>"`, `agent_id="test_e2e_<uuid>"`). Cleanup runs in
`finally` and logs warnings on failure rather than failing the suite —
orphaned test memories are scoped and harmless.

### CI workflow

```
  .github/workflows/ci.yml
  ────────────────────────
  on: push, pull_request
  jobs:
    lint-and-test:
      runs-on: ubuntu-latest
      steps:
        - checkout
        - setup python 3.12
        - install: uv sync --group dev  (or pip install -e ".[dev]")
        - ruff check
        - mypy src/
        - pytest -m "not e2e"
```

E2e is excluded by the `e2e` marker. CI does not need LM Studio, a
container, or any external service. The integration layer's fake server
runs entirely in-process (background thread).

## Goals / Non-Goals

**Goals:**
- Characterize the full server surface (10 tools, helpers, client, schemas)
  with automated tests that pass against the current sync code
- Make the `async-tool-execution` refactor's thesis testable (event loop
  yields during I/O; concurrent calls don't corrupt)
- Make the refactor's mechanical risks detectable (dropped `await`, misbound
  `functools.partial` kwargs, per-status error propagation through the
  `await` boundary, timeout kwarg survival)
- Establish CI that runs lint + typecheck + tests on every push/PR
- Structure the suite for additive growth — `batch-write-guardrails` can
  extend it without reorganizing

**Non-Goals:**
- Testing the MCP transport layer in CI (e2e is gated, needs real stack)
- Asserting on log output (warning text for env-vs-session-config conflicts,
  whitespace-only `MEM0_DEFAULT_AGENT_ID`) — the resolved values are
  asserted, the log lines are not; `caplog` assertions can be added later
- Changing any production code, including refactoring `server.py` to read
  env lazily instead of at import time
- Performance benchmarking (the `perf_*.py` scripts already cover this)
- Testing the Smithery decorator fallback path or `main()` / stdio transport

## Decisions

### 1. Real-socket Starlette + uvicorn fake server over mocks

**Choice**: A Starlette app served by `uvicorn.Server` on `127.0.0.1:0` in a
background thread, with handlers that call `time.sleep(N)` to simulate
blocking I/O.

**Why**: The refactor's thesis ("event loop yields during HTTP I/O") requires
real blocking I/O at the `requests.Session.request` call site. Mocks return
instantly — there's nothing to yield for. A real-socket server produces the
actual blocking behavior the refactor changes.

**Alternatives considered**:

- *Mock / patch `requests.Session.request`*: Cannot test concurrency thesis.
  Mock returns instantly; sync and async code behave identically under mocks.
  Rejected — defeats the purpose.

- *`httpx` + `ASGITransport` (no socket)*: Bypasses the blocking socket
  entirely. The request never leaves the event loop. `requests` can't speak
  `ASGITransport`. Tests the fake server's concurrency, not the client's.
  Rejected — can't distinguish sync from async.

- *`aiohttp.test_utils.TestServer`*: Would work but adds `aiohttp` as a new
  dependency. Rejected — unnecessary when Starlette + uvicorn are already
  transitive via `mcp[cli]`.

- *`pytest-httpserver`*: Real-socket plugin, would work. Adds a new dev
  dependency. Rejected — Starlette + uvicorn already available, no need for
  a dedicated plugin.

- *`http.server` (stdlib)*: Would work, no deps. More verbose than
  Starlette for route handling. Rejected — Starlette is already available
  and more ergonomic.

### 2. `pytest-asyncio` with `asyncio_mode = "auto"` over `anyio` pytest plugin

**Choice**: `pytest-asyncio` with `asyncio_mode = "auto"` in
`pyproject.toml`.

**Why**: The test coroutines are asyncio-shaped (`asyncio.create_task`,
`asyncio.gather`, `asyncio.sleep` for concurrency orchestration). The server
uses `anyio` internally, but the tests observe it from the outside via
`requests` (sync HTTP client) and `asyncio` concurrency primitives.
`pytest-asyncio` is the mainstream choice, well-documented, and
`asyncio_mode = "auto"` means every `async def test_` is automatically
treated as an asyncio test without per-function decorators.

**Alternatives considered**:

- *`anyio` pytest plugin*: Consistent with the framework's concurrency model,
  but less ergonomic for `asyncio.gather` / `asyncio.create_task` patterns.
  The tests are testing a library that uses `anyio`, not testing `anyio`
  itself — the test runner doesn't need to match.

- *`pytest-trio`*: Would require trio-shaped tests. Rejected — the
  concurrency tests use `asyncio` primitives.

### 3. E2e hits the real Docker container, not an in-process server

**Choice**: E2e tests hit `localhost:8765` (the running Docker container),
gated on `MEM0_E2E=1`.

**Why**: The refactor touches the sync/async dispatch boundary in FastMCP
(`iscoroutinefunction` check). An in-process server bypasses the MCP
transport (SSE framing, session handling, `mcp-session-id`). Only the real
container exercises the full deployment path. The existing `verify_mcp.py`
already assumes a running container — this matches that pattern.

**Alternatives considered**:

- *In-process `create_server()` + ASGI transport*: Bypasses SSE framing and
  session handling. Defeats the purpose of e2e. Rejected.

- *No e2e layer, rely on `verify_mcp.py`*: The transport path stays
  manually verified. Rejected — the refactor touches the dispatch boundary,
  which is exactly where "upstream handles it" is a famous last word.

### 4. E2e test scope isolation with forgiving cleanup

**Choice**: Each e2e test run uses `user_id="test_e2e_<uuid>"` and
`agent_id="test_e2e_<uuid>"`. Cleanup via `delete_entities` in `finally`,
logging warnings on failure rather than failing the suite.

**Why**: E2e tests write real memories to real mem0 OSS. Scoping isolates
test data from real user data. Forgiving cleanup avoids masking test
failures with cleanup failures (if the server is broken, cleanup also
fails). Orphaned test memories are scoped and harmless.

**Alternatives considered**:

- *No cleanup*: Simplest, but test data accumulates over time. Rejected for
  hygiene.

- *Strict cleanup (fail on cleanup error)*: Masks test failures with cleanup
  failures. Rejected — cleanup is best-effort.

- *Snapshot/restore*: Too complex for this scale. Rejected.

### 5. CI bundled into this change, not a separate change

**Choice**: `.github/workflows/ci.yml` is part of this change.

**Why**: The change is already touching `pyproject.toml` (adding
`pytest-asyncio`, `asyncio_mode`, `testpaths`, markers). A CI workflow that
runs `ruff check && mypy && pytest -m "not e2e"` is ~30 lines. Without CI,
the test suite is only as good as developer discipline — which is how this
repo ended up with `pytest` in dev deps and zero test files. "Automated
regression suite" without automation is a contradiction.

**Alternatives considered**:

- *CI as a separate change*: Keeps this change focused on tests. But
  "let's add CI later" is how repos end up with no CI for years. Rejected.

- *No CI, manual only*: Rejected — defeats the purpose of an automated
  regression suite.

### 6. The concurrency test ships as `xfail(strict=True)`, not as a red test

**Choice**: `test_event_loop_yields_during_write_read_in_parallel` is marked
`pytest.mark.xfail(strict=True)` when it lands, with a comment stating that
`async-tool-execution` must remove the marker.

**Why**: The test is RED on sync code by design — it is the characterization
proof that the refactor is needed. But a permanently red test makes CI red
from the first commit, which trains everyone to ignore CI. `xfail(strict=True)`
keeps the suite green *and* keeps the proof: `strict=True` means the run fails
with `XPASS` the moment the refactor makes the test pass, forcing the marker's
removal instead of letting it silently rot into a test nobody checks.

```
  before async-tool-execution:  xfail   → suite green, proof recorded
  after  async-tool-execution:  XPASS   → suite RED until marker removed
  marker removed:               pass    → refactor proven, guard permanent
```

**Alternatives considered**:

- *Ship it red*: honest but makes CI red on day one. Rejected.
- *Don't write it until the refactor*: loses the "green before, green after"
  characterization property and the proof that the test can distinguish sync
  from async. Rejected.
- *`xfail` without `strict`*: the test would silently xpass forever after the
  refactor and never become a real regression guard. Rejected.

### 7. Tests patch module constants, not environment variables

**Choice**: Tests use `monkeypatch.setattr(server, "ENV_BASE_URL", ...)`
rather than `monkeypatch.setenv("MEM0_BASE_URL", ...)`.

**Why**: `server.py` resolves configuration into module-level constants at
import time — `ENV_API_KEY`, `ENV_BASE_URL`, `ENV_DEFAULT_USER_ID`,
`ENV_DEFAULT_AGENT_ID`, `_WRITE_TIMEOUT`, `_READ_TIMEOUT` are all evaluated
during module execution. By the time a test runs, `os.getenv` has already
been consulted, so `monkeypatch.setenv` has no effect on the values
`_resolve_settings` and `_call` actually read. This is a property of the
existing code, not something the test suite should change.

**Alternatives considered**:

- *`importlib.reload(server)` per test*: works but is slow, and reloading a
  module that runs `load_dotenv()` and builds a FastMCP instance has
  side effects. Rejected as the default; acceptable as a fallback for a
  test that specifically needs import-time behavior.
- *Refactor `server.py` to read env lazily*: would make tests simpler, but
  this change explicitly does not modify production code. Noted as a possible
  future improvement, out of scope here.

### 8. `skip_specs: true` — this is tooling, not a behavior change

**Choice**: The change's `.openspec.yaml` sets `skip_specs: true`.

**Why**: The test suite encodes existing behavior; it does not introduce new
behavior. The existing `scope-defaults` spec describes server behavior. The
tests verify that behavior (among others), but the tests themselves are not
a spec-level capability. This mirrors the `async-tool-execution` change's
`skip_specs: true` (pure refactor, no behavior change).

## Risks / Trade-offs

- **[Fake server latency makes integration tests slow]** → The concurrency
  test needs ~2s of real sleep to prove the loop yields. Other integration
  tests can use near-zero latency. Total integration suite runtime should be
  under 10s. Acceptable for CI.

- **[E2e tests are environment-dependent]** → Gated on `MEM0_E2E=1`. CI
  never runs them. Documented in the skip message. The integration layer
  covers the same mechanics deterministically; e2e adds real-latency and
  transport-layer confidence.

- **[E2e test flakiness from LM Studio]** → LM Studio can 400 on concurrent
  load. The e2e concurrency test uses a second *session* for the read, not
  a concurrent write, so it shouldn't trigger LM Studio load issues. If
  flakiness emerges, the test can be re-run or the assertion bounds
  loosened.

- **[Port conflicts on CI]** → Fake server binds to port `0` (OS-assigned).
  No conflict possible.

- **[uvicorn startup race in tests]** → The fixture must wait for uvicorn to
  bind before yielding. Poll `server.servers[0].sockets` bounded by a 5s
  deadline and raise `RuntimeError` on timeout — an unbounded poll turns a
  startup failure into a hung suite with no diagnostic.

- **[Thread-safety of the shared fake-server state]** → The fake server reads
  `responses` and appends to `received` from uvicorn worker threads; the test
  reads and writes both from the test thread. `responses` overrides are
  written during per-test setup, before any HTTP call, so there is no race
  there. `received` IS mutated concurrently under the concurrency tests, so
  appends are guarded by a `threading.Lock` and tests only read it after
  `asyncio.gather` has joined all calls.

- **[Timing assertions are inherently environment-sensitive]** → The
  integration concurrency bound is 1.0s against a 2.0s write latency — a 2x
  margin rather than the 4x a 0.5s bound would imply, chosen so CI scheduler
  jitter and thread-pool handoff cannot flake it while still failing
  decisively on sync code (which takes ~2.05s). If flakiness still appears,
  raise `write_latency` rather than loosening the bound, so the ratio stays
  wide.

- **[Suite covers the full server surface, so some tests are forward
  investment]** → The chosen scope characterizes all 10 tools, all helpers,
  the client, the cache, and the schemas — including error paths the async
  refactor does not touch. Those tests pay off when `batch-write-guardrails`
  starts mutating error paths (batch limit, retry classification), not
  immediately. Accepted deliberately: the alternative (a refactor-scoped
  minimal suite) left two real gaps — per-status error propagation through
  the new `await` boundary, and `timeout` survival through
  `functools.partial`.

- **[CI adds `pytest-asyncio` as a dev dependency]** → Must verify version
  compatibility with `pytest>=8.3.4` already declared. `pytest-asyncio`
  0.23+ supports pytest 8. Pin to a version published >7 days ago per
  dependency hygiene rules.
