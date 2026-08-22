## Why

Performance testing revealed that `add_memory` with `infer=True` (the default)
takes ~40s per single-record write on local LLM hardware, dominated by an
8,413-token system-prompt prefill in mem0's `ADDITIVE_EXTRACTION_PROMPT`.
Batching 20 messages in one call amortizes that prefill and drops per-record
cost to ~1s — a 40x improvement. But there is no guardrail: an agent told
"batching is faster" has no boundary and will send hundreds of messages in a
single call, which crashes LM Studio (`400 {'error': 'terminated'}`) and
cascades into 502s that make the mem0 server unresponsive.

This change adds batch-size limits, tool-description performance guidance,
transient-error retry, and a write cooldown. It depends on a prerequisite
change (`async-tool-execution`) that converts the MCP server's sync tool
functions to async with thread offloading, because retry delays and cooldowns
implemented with `time.sleep()` in sync tools block the entire event loop —
paralyzing all MCP operations (including reads from other sessions) for the
duration of every delay.

## What Changes

- Add `max_length` validation on `AddMemoryArgs.messages` (driven by
  `MEM0_BATCH_MAX_MESSAGES`, default 20) so Pydantic rejects oversized batches
  before any network call. The error message tells the agent to split.
- Update `add_memory` tool description and the `messages`/`infer` field
  descriptions to communicate the batching performance characteristic, the
  20-message limit, and the `infer=False` fast path.
- Add a write lock (`anyio.Lock`) around write operations (`add_memory`,
  `update_memory`, `delete_memory`, `delete_all_memories`) so concurrent write
  calls are serialized — preventing multiple simultaneous LLM requests to LM
  Studio. Read operations (`search_memories`, `get_memories`, `list_entities`,
  `get_memory`, `get_memory_history`) run without the lock, concurrently with
  each other and with writes.
- Add a batch cooldown: after a batch write (`messages` with >1 entry)
  completes and the write lock is released, the next batch write that acquires
  the lock waits `MEM0_BATCH_COOLDOWN` seconds (default 10) before sending.
  This enforces the measured safe idle time between batch calls. Single-record
  writes (`text=`) bypass the cooldown. The cooldown uses `anyio.sleep()` so
  the event loop stays responsive during the wait.
- Add server-side retry (one attempt after `MEM0_RETRY_DELAY` seconds, default
  10) for HTTP 400 responses whose body contains `"terminated"`. The retry
  happens inside the write lock (holding it during the delay via
  `anyio.sleep()`) so no other write can interfere. Non-400 errors and 400s
  without `"terminated"` are not retried.
- Add `MEM0_BATCH_MAX_MESSAGES` (default 20), `MEM0_RETRY_DELAY` (default 10),
  and `MEM0_BATCH_COOLDOWN` (default 10) env vars.
- Update `AGENTS.md` with performance characteristics, env vars, and the
  root-cause note about the 8.4K-token extraction prompt.

## Capabilities

### New Capabilities
- `batch-write-guardrails`: Schema-level batch-size limits, tool-description
  performance guidance, write serialization, batch cooldown, and
  transient-error retry for the `add_memory` write path.

### Modified Capabilities
<!-- None. No prior specs exist in this repo. -->

## Impact

- **Prerequisite**: `async-tool-execution` change must be applied first. That
  change converts all tool functions to `async def` and offloads
  `requests.post/get/...` calls to `anyio.to_thread`. Without it, the
  `anyio.Lock`, `anyio.sleep()` cooldown, and `anyio.sleep()` retry delay in
  this change would either not compile (sync functions can't `await`) or would
  need to fall back to `time.sleep()` (blocking the event loop).
- **Code**: `src/mem0_mcp_server/schemas.py` — `AddMemoryArgs.messages` gains
  `max_length`. `src/mem0_mcp_server/server.py` — tool/field descriptions
  updated; `import time` added (not currently imported) for the cooldown clock;
  write lock added around write operations; batch cooldown logic;
  `Mem0OSSClient._call` or the tool layer gains retry for transient 400
  "terminated" errors, with defensive body decoding so classifying an error
  cannot itself raise; three new env vars read at startup.
- **Env**: three new optional variables, `MEM0_BATCH_MAX_MESSAGES` (default
  20), `MEM0_RETRY_DELAY` (default 10), `MEM0_BATCH_COOLDOWN` (default 10).
  No new required variables.
- **Behavior**: backward compatible for all calls with ≤20 messages. Calls
  with >20 messages receive a validation error. Concurrent write calls are
  serialized (previously they blocked each other accidentally via the event
  loop; now they serialize via an explicit lock with responsive reads in
  parallel). The cooldown adds up to 10s latency to consecutive batch writes;
  single-record writes and reads are unaffected. The retry is transparent.
- **Docs**: `AGENTS.md` "Environment variables" section gains three bullets;
  a new "Performance characteristics" subsection documents the batching
  finding, the 8.4K-token prompt root cause, the LM Studio stability
  threshold, and the write serialization model.
- **No upstream mem0 fork required.** This is MCP-layer only.
