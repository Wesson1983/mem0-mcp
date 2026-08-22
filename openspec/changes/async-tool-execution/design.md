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

**Alternative considered**: Switch from `requests` to `httpx` (async HTTP
client). Rejected — `requests` is used throughout `Mem0OSSClient` with session
reuse, header management, and error handling that would all need rewriting.
The thread offload is a one-line wrapper that achieves the same event-loop
yielding without changing the HTTP layer.

**Alternative considered**: Use `asyncio.to_thread()`. Rejected — works but
couples to asyncio backend. `anyio.to_thread.run_sync` is backend-agnostic and
already imported by FastMCP.

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
body in `anyio.to_thread.run_sync`. Rejected — that would offload validation,
Pydantic model construction, and `_resolve_settings` to the thread too, which
is unnecessary (those are CPU-fast). Only the HTTP call needs offloading.

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
