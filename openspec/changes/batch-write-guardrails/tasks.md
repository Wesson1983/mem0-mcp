## 1. Environment variables

- [ ] 1.1 Add `MEM0_BATCH_MAX_MESSAGES` env var read at module load in `server.py` (default 20) using the existing `_int_env` helper. Verify by starting the server and confirming the value is accessible.
- [ ] 1.2 Add `MEM0_RETRY_DELAY` env var read at module load in `server.py` (default 10) using `_int_env`. Verify by starting the server and confirming no errors.
- [ ] 1.3 Add `MEM0_BATCH_COOLDOWN` env var read at module load in `server.py` (default 10) using `_int_env`. Verify by starting the server and confirming no errors.

## 2. Batch size validation

- [ ] 2.1 Add `max_length` to the `messages` field in `AddMemoryArgs` in `schemas.py`, driven by the `MEM0_BATCH_MAX_MESSAGES` value. Verify that constructing `AddMemoryArgs(messages=[...25 items...])` raises `ValidationError` when the limit is 20.
- [ ] 2.2 Verify that constructing `AddMemoryArgs(messages=[...20 items...])` succeeds when the limit is 20.
- [ ] 2.3 Verify that `AddMemoryArgs(text="...", messages=None)` succeeds (single-message path unaffected).

## 3. Tool description updates

- [ ] 3.1 Update the `add_memory` tool `description` string in `server.py` to mention that batching multiple messages in one call is faster, state the max batch size, and advise waiting between consecutive batch calls. Verify by calling `tools/list` over MCP and checking the description text.
- [ ] 3.2 Update the `messages` field `Field(description=...)` in `server.py` to mention batching performance and the max batch size. Verify via `tools/list` that the field description contains the batch guidance.
- [ ] 3.3 Update the `infer` field `Field(description=...)` in `server.py` to state that `False` skips LLM extraction and is faster. Verify via `tools/list` that the field description mentions the tradeoff.

## 4. Write serialization lock

- [ ] 4.1 Add `import time` to `server.py` (not currently imported; needed for `time.monotonic()` in the cooldown). `anyio` and `functools` are already imported by the `async-tool-execution` change. Add a module-level `_WRITE_LOCK = anyio.Lock()`. Verify the import succeeds and the lock is constructed at module load.
- [ ] 4.2 Wrap `add_memory`'s write path with `async with _WRITE_LOCK:`, placed AFTER Pydantic validation and `_resolve_settings` (those are CPU-fast and must not hold the lock) and covering the cooldown wait, the HTTP request, and the retry. Verify a validation failure returns without ever acquiring the lock, and that a valid call holds the lock from the cooldown check through the final response.
- [ ] 4.3 Wrap `update_memory`'s write path with `async with _WRITE_LOCK:`. Verify the lock is acquired before the `await self._client(...).update(...)` call.
- [ ] 4.4 Wrap `delete_memory`'s write path with `async with _WRITE_LOCK:`. Verify the lock is acquired before the `await self._client(...).delete(...)` call.
- [ ] 4.5 Wrap `delete_all_memories`'s write path with `async with _WRITE_LOCK:`. Verify the lock is acquired before the `await self._client(...).delete_all(...)` call.
- [ ] 4.6 Verify that read tools (`search_memories`, `get_memories`, `list_entities`, `get_memory`, `get_memory_history`) do NOT acquire `_WRITE_LOCK`.

## 5. Batch cooldown

- [ ] 5.1 Add a module-level `_LAST_BATCH_END: float = 0.0` timestamp in `server.py`. Verify the variable is initialized.
- [ ] 5.2 In `add_memory`, after acquiring the write lock and before sending the HTTP request, if `messages` has >1 entry and `time.monotonic() - _LAST_BATCH_END < _BATCH_COOLDOWN`, `await anyio.sleep(_BATCH_COOLDOWN - elapsed)` to enforce the cooldown. Verify the sleep uses `anyio.sleep` (async, non-blocking) not `time.sleep`. Verify that a long previous batch (e.g. 60s, longer than the cooldown) results in no wait, because its completion timestamp is already older than `_BATCH_COOLDOWN` — long writes self-cooldown.
- [ ] 5.3 Set `_LAST_BATCH_END = time.monotonic()` after the batch write's HTTP attempt finishes, in a `finally` around the request+retry block so it runs on success, upstream error, and exception alike. It must NOT be set when the call never reached the HTTP layer (Pydantic validation failure, oversized batch), so keep the assignment inside the post-lock request block, not at function scope. Verify the timestamp advances after a failed upstream write and does not advance after a rejected oversized batch.
- [ ] 5.4 Verify that single-record writes (`text=` with no `messages`) skip both the cooldown check and the timestamp update.

## 6. Transient error retry

- [ ] 6.1 Add retry logic in the write path (inside the write lock): when the HTTP response is 400 and `"terminated"` appears in the response body, `await anyio.sleep(_RETRY_DELAY)` and retry the HTTP request once via `anyio.to_thread.run_sync`. Return the retry response regardless of outcome. Verify the retry uses `anyio.sleep` (async, non-blocking) and holds the write lock during the delay.
- [ ] 6.2 Make the "terminated" detection defensive: read the body via `response.text` inside a `try/except Exception`, treat a missing, empty, or undecodable body as "not transient" (no retry), and match case-insensitively on the decoded text. Verify that a 400 with an empty body, a non-UTF-8 body, and an HTML error page each return immediately without raising and without retrying.
- [ ] 6.3 Verify that a 400 without "terminated" in the body is returned immediately without retry (no sleep occurs).
- [ ] 6.4 Verify that a 500 error is returned immediately without retry.
- [ ] 6.5 Verify that read operations are not retried (retry applies only to write methods).

## 7. AGENTS.md documentation

- [ ] 7.1 Add `MEM0_BATCH_MAX_MESSAGES`, `MEM0_RETRY_DELAY`, and `MEM0_BATCH_COOLDOWN` to the "Environment variables" section in `AGENTS.md`. Verify the bullets match the existing format and defaults.
- [ ] 7.2 Add a "Performance characteristics" subsection to `AGENTS.md` documenting: the 8.4K-token extraction prompt root cause, the ~40s single-write / ~1s batched-write numbers, the 20-message stability threshold for LM Studio, the write serialization model, the batch cooldown, and the retry behavior. Verify the section reads clearly and cites measured numbers from the perf test.

## 8. End-to-end verification

- [ ] 8.1 Rebuild the Docker image and restart the container. Verify the server starts without errors and `tools/list` returns 10 tools with the updated `add_memory` description.
- [ ] 8.2 Call `add_memory` via MCP with 25 messages and verify it returns a validation error mentioning the 20-message limit, without sending an HTTP request to the upstream server (check mem0 server logs for no `POST /memories`).
- [ ] 8.3 Call `add_memory` via MCP with 20 messages and verify it succeeds (HTTP 200 from upstream, results returned).
- [ ] 8.4 Run `perf_batch_add_memory.py 5 20 --delay 0` (no manual delay) and verify that the cooldown + retry logic handles LM Studio stability without 502 cascades. Confirm all 5 calls succeed and total wall-clock time is approximately 5×20s + 4×10s cooldown = ~140s.
- [ ] 8.5 Verify concurrent read during write: start an `add_memory` call (20s+), and while it is in flight, call `search_memories` from a second MCP session. Verify the read completes without waiting for the write to finish (the write lock does not block reads).
