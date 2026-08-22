## Why

All MCP tool functions in `server.py` are sync (`def add_memory`, `def
search_memories`, etc.). FastMCP calls sync tools directly in the async event
loop (`return fn(**arguments)` — no `anyio.to_thread`, no executor). Each tool
calls `requests.post/get/...`, which blocks the entire event loop for the
duration of the HTTP call (0.05s for reads, 20-40s for writes). During a write,
all other MCP operations — reads from the same session, any operation from any
other connected session — are frozen. This makes the server unusable for
concurrent sessions and blocks the prerequisite for non-blocking retry and
cooldown logic in the `batch-write-guardrails` change.

## What Changes

- Convert all 10 tool functions from `def` to `async def`.
- Offload every `requests.Session.request()` call in `Mem0OSSClient._call`
  to a thread via `anyio.to_thread.run_sync()`, so the event loop is not
  blocked during HTTP I/O.
- `Mem0OSSClient._call` becomes `async def`; all callers (`add`, `search`,
  `list_memories`, `get`, `update`, `delete`, `delete_all`, `history`,
  `list_entities`, `delete_entity`) become `async def` and are `await`ed.
- No externally observable behavior change. Tool signatures, return types,
  error handling, env vars, and HTTP paths are identical. The only difference
  is that the event loop yields during I/O instead of blocking.

## Capabilities

### New Capabilities
<!-- None. This is a pure internal refactor with no spec-level behavior change. -->

### Modified Capabilities
<!-- None. No externally observable behavior changes. -->

## Impact

- **Code**: `src/mem0_mcp_server/server.py` — all 10 tool functions become
  `async def`; `Mem0OSSClient._call` and all its wrapper methods become
  `async def`; `requests.Session.request` wrapped in `anyio.to_thread.run_sync`.
  No changes to `schemas.py`.
- **Dependencies**: `anyio` is already a transitive dependency via
  `mcp[cli]` (FastMCP imports it). No new packages.
- **Behavior**: no change. Tools return the same results, same errors, same
  timing characteristics for individual calls. The only observable difference
  is that concurrent operations no longer block each other — reads complete
  while a write is in flight, and multiple sessions can be served
  simultaneously.
- **Docs**: no `AGENTS.md` changes required (no new env vars, no new
  behavior to document).
- **Prerequisite for**: `batch-write-guardrails` — that change's `anyio.Lock`,
  `anyio.sleep()` cooldown, and `anyio.sleep()` retry delay require async tool
  functions. Without this change, those primitives either don't compile (sync
  functions can't `await`) or degrade to `time.sleep()` (blocking the event
  loop, defeating the purpose).
