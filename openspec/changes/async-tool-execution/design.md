## Context

The MCP server uses FastMCP (from `mcp[cli]` <2.0.0). FastMCP's tool dispatch
calls sync tool functions directly in the async event loop —
`return fn(**arguments)` in `func_metadata.py` — with no thread offload. Every
tool in `server.py` is `def` (sync) and calls `requests.Session.request()`,
which blocks the calling thread. The result: during any tool call, the entire
Starlette/uvicorn event loop is frozen. A 20-40s `add_memory` write blocks all
reads, all other sessions, and all health checks for its full duration.

`anyio` is already available as a transitive dependency (FastMCP imports it
extensively). `anyio.to_thread.run_sync()` offloads a sync callable to a
worker thread and awaits its result, yielding control to the event loop during
the wait.

See proposal.md for motivation and the prerequisite relationship to
`batch-write-guardrails`.

## Architecture

### Current state — synchronous tools block the event loop

```
 Agent session A ─┐
                  ├─► FastMCP dispatch ─► sync tool fn ─► requests.post ─► Mem0 OSS
 Agent session B ─┘   (anyio loop)       (blocks loop)   (20-40s write)
                                                       ▲
                                                       │
                       All other operations FROZEN for the full HTTP duration:
                       reads from session A, any op from session B, health checks
```

Key points:

- FastMCP's `func_metadata.py` calls sync tools via `return fn(**arguments)` —
  no `anyio.to_thread`, no executor.
- `requests.Session.request()` blocks the calling thread, which is the event
  loop thread.
- Reads (0.05-2s) and writes (20-40s) both block, but writes dominate.
- Concurrent sessions are accidentally serialized, not concurrently served.

### Proposed state — async tools offload HTTP I/O to worker threads

```
 Agent session A ─┐
                  ├─► FastMCP dispatch ─► async tool fn ─┐
 Agent session B ─┘   (anyio loop)       (awaits)        │
                                                         ▼
                                          anyio.to_thread.run_sync(bound_callable)
                                                         │
                                          ┌──────────────┴──────────────┐
                                          │  anyio worker thread pool   │
                                          │  (default ~40 threads)      │
                                          └──────────────┬──────────────┘
                                                         │
                                                  requests.post / get
                                                         │
                                                         ▼
                                                      Mem0 OSS
                                         (event loop FREE during I/O wait)
```

Key points:

- Only `requests.Session.request()` is offloaded. Validation, Pydantic model
  construction, `_resolve_settings`, and error handling stay on the event loop
  (CPU-fast, no I/O).
- `functools.partial` binds the keyword arguments
  (`method`, `url`, `params`, `json`, `timeout`) before the offload because
  `anyio.to_thread.run_sync` forwards only positional args.
- The event loop yields during the `await`, so other sessions and other tools
  run concurrently.
- `requests.RequestException` raised in the thread propagates to the `await`
  point, where the existing `try/except` catches it unchanged.

### Future state — with `batch-write-guardrails` (prerequisite relationship)

```
 Agent session A ─┐
                  ├─► async tool fn ─► [validate batch size <= MEM0_BATCH_MAX_MESSAGES]
 Agent session B ─┘                    │  (reject oversized before any I/O)
                                      ▼
                                 [acquire anyio.Lock]   ◄── serializes writes only
                                      │
                                      ▼
                                 [cooldown check]       ◄── anyio.sleep gap between
                                      │                     completed batch writes
                                      ▼
                                 anyio.to_thread.run_sync(requests.post)
                                      │
                                      ▼
                                 [on HTTP 400 + "terminated": retry once]
                                      │   ◄── anyio.sleep(MEM0_RETRY_DELAY)
                                      ▼
                                 [release lock, update last-write timestamp]
```

This change only produces the middle layer (async tools + thread offload). The
lock, cooldown, and retry are added by `batch-write-guardrails`, which depends
on this change because those primitives require `async def` tools to use
`anyio.Lock` and `anyio.sleep` without blocking the loop.

## Implementation Options

Five approaches were considered. Options are ordered by preference.

### Option A — `anyio.to_thread.run_sync` wrapping existing `requests` calls (CHOSEN)

Offload only the `requests.Session.request()` call inside `Mem0OSSClient._call`
to a worker thread via `anyio.to_thread.run_sync(functools.partial(...))`. All
tool functions and client wrappers become `async def` and propagate the
`await`.

| Pros | Cons |
|------|------|
| Minimal code change — one offload point in `_call` | `requests.Session` is not documented thread-safe for concurrent writes on the same session object |
| No new dependencies (`anyio` already transitive via `mcp[cli]`) | Default thread pool (~40) could exhaust under extreme concurrent write bursts |
| Backend-agnostic (works with asyncio or trio) | Concurrency now actually happens — exposes any latent non-thread-safe code paths |
| Preserves all existing error handling, headers, session reuse | Slightly more complex stack traces (thread boundary) |
| Unlocks `batch-write-guardrails` (`anyio.Lock`, `anyio.sleep`) | — |
| No HTTP-layer rewrite | — |

### Option B — Switch from `requests` to `httpx` async client

Replace `requests.Session` with `httpx.AsyncClient` throughout
`Mem0OSSClient`. Tool functions and wrappers become `async def` and `await`
the `httpx` calls directly — no thread offload needed.

| Pros | Cons |
|------|------|
| Native async — no thread pool, no `functools.partial` binding | Rewrites the entire HTTP layer of `Mem0OSSClient` (session, headers, auth, error handling) |
| No thread-safety concerns — `httpx.AsyncClient` is concurrency-safe | New direct dependency (`httpx`) — must verify version compatibility with `mcp[cli] <2.0.0` |
| Cleaner concurrency model | Larger diff, higher regression risk |
| — | Does not change the prerequisite relationship — `batch-write-guardrails` still needs `async def` tools |

Rejected: the blocking call is a single line in `_call`; rewriting the whole
HTTP layer is disproportionate to the problem.

### Option C — `asyncio.to_thread`

Same offload strategy as Option A but using `asyncio.to_thread` instead of
`anyio.to_thread.run_sync`.

| Pros | Cons |
|------|------|
| Stdlib — no framework coupling | Couples the code to the asyncio backend specifically |
| Same minimal-diff shape as Option A | FastMCP uses `anyio` exclusively; mixing in raw `asyncio` primitives is inconsistent with the framework |
| — | `anyio.to_thread.run_sync` is already imported by FastMCP and works on any backend |

Rejected: works but is less consistent with the framework's concurrency model
than Option A.

### Option D — Wrap the entire tool function body in a thread executor

Keep tool functions sync, but wrap the entire body (validation, Pydantic
construction, `_resolve_settings`, HTTP call, error handling) in
`anyio.to_thread.run_sync`.

| Pros | Cons |
|------|------|
| Smallest possible diff at the tool-function level | Offloads CPU-fast work (validation, model construction) that doesn't need offloading |
| No need to make wrappers async | Larger thread hold time — each thread is occupied for the full tool duration, not just I/O |
| — | Obscures the boundary between fast in-loop work and slow I/O |
| — | Harder to compose with `batch-write-guardrails` (lock acquisition would happen inside the thread, not on the loop) |

Rejected: offloads work that doesn't need it and complicates the
`batch-write-guardrails` lock/cooldown placement.

### Option E — Background task queue with dedicated worker(s)

Tool functions enqueue write requests onto an `asyncio.Queue`; a fixed pool of
async workers drains the queue and performs the HTTP calls. Reads stay inline.

| Pros | Cons |
|------|------|
| Explicit concurrency control — bounded workers, backpressure | Significant new machinery (queue, workers, lifecycle, shutdown) |
| Natural fit for write serialization | Changes the tool contract — calls return before the write completes (or need a future/awaitable result) |
| — | Out of scope for this change; `batch-write-guardrails` covers write serialization with a simple lock |
| — | Over-engineered for a single-user MCP server |

Rejected: solves a different problem (write serialization and backpressure)
that is explicitly the `batch-write-guardrails` change's scope, and changes the
synchronous tool contract.

### Selection rationale

Option A is chosen because:

1. The blocking call is a single line (`self._session.request()` inside
   `_call`); the fix is proportional to the problem.
2. `anyio` is already a transitive dependency — no new packages.
3. It preserves the existing HTTP layer, error handling, and session reuse
   verbatim.
4. It produces the `async def` tool functions that
   `batch-write-guardrails` requires for `anyio.Lock` and `anyio.sleep`.
5. The thread-safety and pool-exhaustion cons are mitigated by
   `batch-write-guardrails`'s write lock (serializes writes to one at a time)
   and by the single-user workload profile.

## Goals / Non-Goals

**Goals:**
- Stop blocking the event loop during HTTP I/O in tool functions
- Enable concurrent read operations during writes
- Enable multiple MCP sessions to be served simultaneously
- Preserve identical tool behavior (same inputs, outputs, errors, timing per call)

**Non-Goals:**
- Adding write serialization / locks (that's `batch-write-guardrails`)
- Adding retry, cooldown, or batch limits (that's `batch-write-guardrails`)
- Changing the HTTP client library (`requests` stays; it's wrapped in threads)
- Changing tool signatures, return types, or error formats
- Changing env vars or configuration

## Decisions

### 1. `anyio.to_thread.run_sync()` over `asyncio.to_thread()`

**Choice**: Use `anyio.to_thread.run_sync()` to offload
`requests.Session.request()`.

**Why**: The MCP server's event loop is managed by `anyio` (FastMCP uses
`anyio` exclusively, not raw `asyncio`). Using `anyio.to_thread` is consistent
with the framework's concurrency model and works with any `anyio` backend
(asyncio or trio). `asyncio.to_thread` would work but couples the code to the
asyncio backend specifically.

**Alternatives considered**: See Implementation Options above for the full
comparison of `httpx` (Option B), `asyncio.to_thread` (Option C),
whole-function offload (Option D), and a background task queue (Option E).
All were rejected in favor of Option A; the rationale is summarized there.

### 2. `Mem0OSSClient._call` becomes async; wrapper methods become async

**Choice**: `_call` becomes `async def` and offloads the
`requests.Session.request` call via `await anyio.to_thread.run_sync(...)`. All
wrapper methods (`add`, `search`, `list_memories`, `get`, `update`, `delete`,
`delete_all`, `history`, `list_entities`, `delete_entity`) become `async def`
and `await self._call(...)`. Tool functions `await self._client(...).add(body)`
etc.

**Argument passing**: `anyio.to_thread.run_sync` has the signature
`run_sync(func, *args, abandon_on_cancel=..., cancellable=..., limiter=...)` —
it forwards only *positional* arguments to `func`, and its own keyword
parameters are reserved. Passing `params=`, `json=`, or `timeout=` through
`run_sync` raises `TypeError: got an unexpected keyword argument`. The existing
`_call` invokes `self._session.request` with keyword arguments, so they must be
bound before the offload:

```python
bound = functools.partial(
    self._session.request,
    method=method,
    url=self._url(path),
    params=params,
    json=json_body,
    timeout=timeout,
)
response = await anyio.to_thread.run_sync(bound)
```

This requires adding `import functools` to `server.py`.

**Why**: The blocking call is `self._session.request()` inside `_call`. That's
the single point to offload. Making the wrappers async is mechanical — they
just propagate the `await`. Tool functions must be `async def` to `await` the
client methods, and FastMCP already supports async tool functions (it checks
`inspect.iscoroutinefunction` and calls `await fn(...)` if true).

**Alternative considered**: Keep `_call` sync, wrap the entire tool function
body in `anyio.to_thread.run_sync` (Option D in Implementation Options).
Rejected — that would offload validation, Pydantic model construction, and
`_resolve_settings` to the thread too, which is unnecessary (those are
CPU-fast). Only the HTTP call needs offloading.

### 3. `requests.RequestException` handling stays in the thread

**Choice**: The `try/except requests.RequestException` block stays inside
`_call`, wrapping the `anyio.to_thread.run_sync` call. If the thread raises,
`anyio` propagates the exception to the `await` point, and the existing
`except` catches it.

**Why**: The exception handling logic (logging, cache clearing, returning
`_error(...)`) is unchanged. `anyio.to_thread.run_sync` propagates exceptions
from the thread to the caller, so the existing `try/except` works as-is.

### 4. No thread pool size limit

**Choice**: Use `anyio.to_thread.run_sync()` without an explicit
`limiter` — rely on `anyio`'s default thread pool (typically 40 threads).

**Why**: The MCP server is a single-user tool (one agent, possibly a few
sessions). The default thread pool is more than sufficient. Adding a limiter
is premature optimization. If concurrent write serialization is needed, that's
the `batch-write-guardrails` change's `anyio.Lock`, not a thread limiter.

## Risks / Trade-offs

- **[Thread safety of `requests.Session`]** → `requests.Session` is documented
  as not thread-safe for concurrent use of the same session object across
  threads. However, `anyio.to_thread.run_sync` runs one callable at a time per
  call — it doesn't run the same session concurrently. The risk is if two
  tool calls run in parallel threads on the same `Mem0OSSClient` instance
  (same cached session). In practice, the `_client` cache returns the same
  client for the same (base_url, api_key), so two concurrent calls could use
  the same session from two threads. Mitigation: `requests.Session` is
  generally safe for concurrent reads (GET) but unsafe for concurrent writes.
  The `batch-write-guardrails` change adds a write lock that serializes
  writes, eliminating this risk. For this change alone, concurrent reads on
  the same session are low-risk (no shared mutable state in GET). If this
  proves problematic, each `to_thread` call can create a per-call session
  from `requests.Session()` (overhead: TCP connection per call, no
  keep-alive).

- **[Default thread pool exhaustion]** → If an agent fires 50 concurrent
  `add_memory` calls, 50 threads are spawned (up to the pool limit), each
  holding a `requests.post` for 20-40s. The pool could exhaust. Mitigation:
  this is an edge case for a single-user MCP server. The
  `batch-write-guardrails` write lock will serialize writes, limiting
  concurrent write threads to 1. Read operations are fast (0.05-2s) and
  won't exhaust the pool.

- **[Behavioral difference: concurrent operations now actually run
  concurrently]** → Previously, operations were accidentally serialized by
  the blocked event loop. After this change, reads and writes from different
  sessions run in parallel. This is the intended improvement, but it means
  the upstream mem0 server could receive concurrent requests it previously
  didn't. The mem0 OSS server (FastAPI/uvicorn) handles concurrent requests
  natively, so this is safe. The only concern is concurrent LLM requests to
  LM Studio, which `batch-write-guardrails` addresses with a write lock.

## Pros and Cons (chosen approach)

**Pros:**

- Minimal, proportional change — one offload point in `_call`, mechanical
  `async def` / `await` propagation through wrappers and tools.
- No new dependencies (`anyio` is already transitive via `mcp[cli]`;
  `functools` is stdlib).
- Backend-agnostic — works with any `anyio` backend (asyncio or trio),
  consistent with FastMCP's concurrency model.
- Preserves the existing HTTP layer, session reuse, header/auth handling,
  and `requests.RequestException` error path verbatim.
- Unlocks `batch-write-guardrails` — produces the `async def` tool functions
  that `anyio.Lock` and `anyio.sleep` require.
- No externally observable behavior change — same tool signatures, return
  types, error formats, env vars, and per-call timing.

**Cons:**

- `requests.Session` is not documented thread-safe for concurrent writes on
  the same session object. Mitigated by `batch-write-guardrails`'s write lock;
  until that change lands, concurrent writes on a cached session are a latent
  risk (concurrent reads are low-risk).
- Default `anyio` thread pool (~40 threads) could exhaust under extreme
  concurrent write bursts. Mitigated by the single-user workload and by
  `batch-write-guardrails`'s write lock (serializes writes to one at a time).
- Concurrency now actually happens — any latent non-thread-safe code in the
  server or upstream could surface. The known surface (session reuse) is
  addressed above; unknown surfaces would surface as bugs to investigate.
- Stack traces across the thread boundary are slightly less direct, though
  `anyio` propagates exceptions faithfully.
- Does not, by itself, add write serialization, retry, cooldown, or batch
  limits — those remain the `batch-write-guardrails` change's scope. This
  change is necessary but not sufficient for safe sustained batch traffic.
