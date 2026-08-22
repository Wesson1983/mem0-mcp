## Context

The mem0 MCP server's `add_memory` tool wraps `POST /memories` on the
self-hosted mem0 OSS server. With `infer=True` (the default), each call
triggers one LLM fact-extraction call inside mem0, which sends an 8,413-token
system prompt (`ADDITIVE_EXTRACTION_PROMPT`) plus the user's messages. On local
LLM hardware (LM Studio serving qwen3.5-9b), a single-record write takes ~40s
(prefill-dominated). Batching 20 messages in one call amortizes the prefill
and drops per-record cost to ~1s.

The upstream mem0 OSS server has no batch-size limit. LM Studio, however,
terminates generations that run too long with `400 {'error': 'terminated'}`,
which mem0 surfaces as a 502. Without a guardrail, an agent that learns
"batching is faster" will send unbounded batches and crash LM Studio.

This change builds on the `async-tool-execution` prerequisite, which converted
all tool functions to `async def` and offloaded `requests.Session.request` to
`anyio.to_thread.run_sync`. Without that prerequisite, the `anyio.Lock`,
`anyio.sleep()` cooldown, and `anyio.sleep()` retry delay in this change would
block the event loop.

See proposal.md for the full motivation and measured numbers.

## Goals / Non-Goals

**Goals:**
- Prevent agents from sending batches large enough to crash LM Studio
- Make the batching optimization discoverable from tool descriptions alone
- Serialize concurrent writes so LM Studio never receives simultaneous LLM
  requests
- Enforce a cooldown between batch writes to match the measured safe idle time
- Transparently recover from transient LM Studio "terminated" errors
- Allow operators to tune the batch limit, cooldown, and retry delay without
  code changes
- Keep reads responsive during writes, cooldowns, and retry delays

**Non-Goals:**
- Modifying the upstream mem0 OSS server or its extraction prompt
- Adding a batch/queue tool (the existing `add_memory` with `messages` already
  supports batching; this change only limits and documents it)
- Creating a Devin skill for batch strategy (the schema limit + tool
  descriptions make a skill unnecessary for safety; a skill could be added
  later for advanced optimization guidance)
- Retrying non-transient errors (network failures, 500s, validation 400s)
- Auto-splitting oversized batches server-side (the error tells the agent to
  split; auto-splitting would hide the batch boundary and complicate
  `run_id`/`metadata` semantics)
- Limiting concurrent reads (reads are fast and don't involve LM Studio)

## Decisions

### 1. Pydantic `max_length` on `AddMemoryArgs.messages` for the hard limit

**Choice**: Add `max_length` to the `messages` field in `AddMemoryArgs`,
driven by `MEM0_BATCH_MAX_MESSAGES` (default 20).

**Why**: Validation fires inside `AddMemoryArgs(...)` construction in
`add_memory`, before any HTTP call. The agent gets a clean Pydantic
`ValidationError` that the MCP framework surfaces as a tool error. No network
resources are wasted on a request that would crash LM Studio.

**Alternative considered**: Validate inside `add_memory`'s function body with
a manual `if len(messages) > limit: return _error(...)`. Rejected because
Pydantic validation is the established pattern in this codebase (every field
uses `Field(...)` with constraints) and fires earlier, before the
`ToolMessage(**msg)` loop runs.

**Alternative considered**: Auto-split oversized batches into multiple
`POST /memories` calls inside the MCP server. Rejected because it hides the
boundary from the agent (the agent thinks one call = one batch), complicates
`run_id`/`metadata` scoping (each sub-batch would need its own or share one),
and makes error reporting ambiguous (which sub-batch failed?).

### 2. `anyio.Lock` for write serialization

**Choice**: A module-level `anyio.Lock` (`_WRITE_LOCK`) acquired at the start
of every write tool function (`add_memory`, `update_memory`, `delete_memory`,
`delete_all_memories`) and released when the function returns. Read tool
functions do not acquire the lock.

**Why**: After the `async-tool-execution` change, concurrent tool calls run in
parallel threads. Without serialization, two concurrent `add_memory` calls
would send two simultaneous `POST /memories` requests, triggering two
concurrent LLM extractions in LM Studio — guaranteed to produce "terminated"
errors. The lock ensures only one write is in flight at a time. Reads don't
need the lock because they don't trigger LLM generation and are fast (0.05-2s).

**Alternative considered**: A `anyio.Semaphore(1)` — functionally identical to
a Lock for this use case. Lock is clearer in intent ("serialize writes").

**Alternative considered**: No lock, rely on the cooldown alone. Rejected —
the cooldown fires after a batch completes, but two batch calls arriving
simultaneously would both pass the cooldown check (no previous batch
timestamp) and both send concurrently.

### 3. Batch cooldown via `anyio.sleep()` with timestamp tracking

**Choice**: A module-level `_LAST_BATCH_END: float` timestamp. When a batch
write (`messages` with >1 entry) acquires the write lock, it checks
`time.monotonic() - _LAST_BATCH_END`. If less than `MEM0_BATCH_COOLDOWN`
(default 10) seconds have elapsed, it `await anyio.sleep(remaining)` before
proceeding. After the batch's HTTP attempt finishes it sets `_LAST_BATCH_END =
time.monotonic()` in a `finally`, so the timestamp advances on success,
upstream error, and exception alike. Single-record writes (`text=`) skip both
the check and the update.

The assignment lives inside the post-lock request block, not at function
scope, so a call rejected before the HTTP layer (Pydantic batch-size failure)
does not consume the cooldown window. This matters because otherwise an agent
retrying an oversized batch in a loop would keep resetting the timer and
starve legitimate batch writes.

`_LAST_BATCH_END` initializes to `0.0` rather than `time.monotonic()`, so the
first batch write after process start never waits: `time.monotonic()` is
already far larger than 0 on any running process.

Because the cooldown is measured from completion, a slow batch self-cooldowns —
a 60s write has already given LM Studio 60s of post-generation idle time by the
time it returns, so no additional wait is applied.

**Why**: Our perf test showed 10s of idle time between batch calls eliminated
all "terminated" errors. The cooldown enforces this proactively — before the
failure, not after. `anyio.sleep()` yields to the event loop, so reads and
other non-write operations remain responsive during the cooldown wait. The
cooldown is measured from the *end* of the previous batch, not the start, so
it enforces idle time (the metric that matters), not total spacing.

**Alternative considered**: Cooldown measured from the start of the previous
batch. Rejected — batch call duration varies (20-60s), so a fixed total
spacing would over-wait for long calls and under-wait for short ones. Idle
time is the stable metric.

**Alternative considered**: No cooldown, rely on retry alone. Rejected — retry
is reactive (fires after failure, wasting 20s on the doomed attempt). The
cooldown is proactive (prevents the failure). Both are needed: cooldown for
the common case, retry for edge cases where cooldown wasn't enough.

### 4. Retry only 400 "terminated", only once, only on writes, while holding lock

**Choice**: When a write operation's HTTP response is HTTP 400 with
`"terminated"` in the body, `await anyio.sleep(MEM0_RETRY_DELAY)` (default 10)
and retry the HTTP request once. The retry happens while holding the write
lock, so no other write can interfere. Return the retry response regardless of
outcome.

**Body inspection must not raise**: reading `response.text` can fail on an
undecodable payload, and the body may be empty or non-text if something other
than LM Studio produced the 400 (a proxy, a gateway, a misconfigured
upstream). The check therefore reads the body inside a `try/except Exception`
and treats absent, empty, or undecodable bodies as non-transient — no retry.
Matching is case-insensitive. The failure mode is deliberately asymmetric: a
missed retry costs one surfaced error, while an exception raised while
classifying an error would replace a useful upstream message with an opaque
crash.

**Why**: The "terminated" error is LM Studio's generation timeout — it's
transient and recoverable. Retrying once after 10s was empirically sufficient.
The retry holds the write lock because releasing it between attempts would
allow another write to start, potentially re-overloading LM Studio. The
`anyio.sleep()` delay keeps the event loop responsive for reads.

**Alternative considered**: Retry with exponential backoff. Rejected — one
retry after 10s was empirically sufficient, and multiple retries would hold
the write lock for 30-60s, blocking all writes for too long.

**Alternative considered**: Release the lock during retry delay. Rejected —
another write could start during the delay and interfere with LM Studio's
recovery.

### 5. Env vars read at module load, not per-call

**Choice**: Read `MEM0_BATCH_MAX_MESSAGES`, `MEM0_RETRY_DELAY`, and
`MEM0_BATCH_COOLDOWN` once at module load time (same pattern as
`MEM0_HTTP_TIMEOUT`, `MEM0_DEFAULT_USER_ID`).

**Why**: Consistent with existing env-var handling. The MCP server runs as a
long-lived process; changing any of these requires a restart, which is the
expected deployment model (Docker container with `--env-file`).

### 6. Tool description updates, not a separate skill

**Choice**: Put batching guidance, the limit, and the spacing advice directly
in the `add_memory` tool description and field descriptions.

**Why**: Tool descriptions are visible to every agent on every `tools/list`
call with zero additional infrastructure. The schema limit provides the safety
boundary; the description provides the hint. Together they're sufficient. The
tool description also advises waiting between batch calls, which the cooldown
enforces server-side as a backstop.

## Risks / Trade-offs

- **[Limit too conservative for fast inference engines]** → Operators using
  vLLM or a remote LLM API can set `MEM0_BATCH_MAX_MESSAGES` higher. The
  default of 20 is tuned for local LM Studio; it's a config, not a constant.

- **[Cooldown adds latency to back-to-back batch writes]** → A batch write
  arriving within `MEM0_BATCH_COOLDOWN` of the previous batch's completion
  waits up to 10s. This is intentional — the alternative (no cooldown) leads
  to "terminated" errors that waste 20s on a doomed attempt plus 10s retry.
  The cooldown is the lesser latency cost. Single-record writes and reads are
  unaffected.

- **[Write lock blocks concurrent writes]** → Two concurrent `add_memory`
  calls serialize instead of running in parallel. This is intentional —
  parallel writes would overload LM Studio. The lock does not affect reads.

- **[Agents may not split batches correctly]** → The validation error message
  states the limit and instructs splitting. Agents that ignore the error and
  retry the same oversized batch get the same error — a safe failure mode.

- **["terminated" string match is fragile]** → LM Studio could change the
  error format. The match is a substring check on the response body, not a
  strict JSON parse, so it tolerates format changes as long as the word
  "terminated" appears. If LM Studio changes the error entirely, the retry
  silently stops firing and the agent sees the raw error — graceful
  degradation, not a crash.

- **[Module-level timestamp is shared across all sessions]** →
  `_LAST_BATCH_END` is a module-level variable, so batch writes from different
  sessions share the same cooldown timeline. This is correct — LM Studio is a
  shared resource, and the cooldown protects it regardless of which session
  triggered the previous batch.
