## Purpose

Limits and guides batch writes through the `add_memory` MCP tool so that agents
batch efficiently without exceeding the upstream LLM inference engine's
generation-time stability threshold, serializes concurrent writes to prevent
simultaneous LLM requests, enforces a cooldown between batch writes, and
transparently retries transient inference-engine failures.

## ADDED Requirements

### Requirement: Batch size validation

The system SHALL reject `add_memory` calls where `messages` contains more than
`MEM0_BATCH_MAX_MESSAGES` entries (default 20). The rejection MUST occur before
any HTTP request is sent to the upstream mem0 server. The error response MUST
state the limit and instruct the caller to split the batch into smaller calls.

#### Scenario: Batch within limit
- **WHEN** `add_memory` is called with `messages` containing 20 entries
- **THEN** the system processes the call normally and forwards it to the upstream server

#### Scenario: Batch exceeds limit
- **WHEN** `add_memory` is called with `messages` containing 25 entries and the limit is 20
- **THEN** the system returns a validation error without sending any HTTP request
- **AND** the error message states the maximum allowed batch size and instructs the caller to split

#### Scenario: Single message unaffected
- **WHEN** `add_memory` is called with `text` set and `messages` unset
- **THEN** the system processes the call normally (the batch limit applies only to `messages`)

#### Scenario: Operator tunes the limit
- **WHEN** `MEM0_BATCH_MAX_MESSAGES` is set to 50 in the environment
- **THEN** the system accepts batches of up to 50 messages and rejects batches of 51 or more

### Requirement: Tool description performance guidance

The `add_memory` tool description and the `messages` and `infer` field
descriptions SHALL communicate that batching multiple messages in a single call
is significantly faster per record than repeated single-record calls, and SHALL
state the maximum batch size. The `infer` field description SHALL state that
setting `infer=False` skips LLM fact extraction, reducing per-call latency at
the cost of storing raw text instead of structured facts. The tool description
SHALL advise waiting between consecutive batch calls to let the inference
engine recover.

#### Scenario: Agent discovers batching from tool list
- **WHEN** an agent calls `tools/list`
- **THEN** the `add_memory` tool's `messages` field description mentions that batching is faster and states the maximum batch size

#### Scenario: Agent discovers infer=False tradeoff
- **WHEN** an agent calls `tools/list`
- **THEN** the `add_memory` tool's `infer` field description mentions that `False` skips LLM extraction and is faster

#### Scenario: Agent discovers batch spacing guidance
- **WHEN** an agent calls `tools/list`
- **THEN** the `add_memory` tool description advises waiting between consecutive batch calls

### Requirement: Write serialization

The system SHALL serialize write operations (`add_memory`, `update_memory`,
`delete_memory`, `delete_all_memories`) using an async lock so that no two
write operations execute concurrently. Read operations (`search_memories`,
`get_memories`, `list_entities`, `get_memory`, `get_memory_history`) SHALL NOT
be blocked by the write lock and SHALL execute concurrently with each other
and with in-flight writes.

#### Scenario: Concurrent writes are serialized
- **WHEN** two `add_memory` calls are in flight simultaneously
- **THEN** the second call waits for the first to complete before sending its HTTP request

#### Scenario: Reads are not blocked by writes
- **WHEN** an `add_memory` call is in flight and a `search_memories` call arrives
- **THEN** the `search_memories` call executes immediately without waiting for the write to complete

#### Scenario: Concurrent reads are not blocked by each other
- **WHEN** two `search_memories` calls arrive simultaneously
- **THEN** both execute concurrently without waiting for each other

### Requirement: Batch cooldown

After a batch write (`add_memory` with `messages` containing more than 1
entry) completes and releases the write lock, the next batch write that
acquires the lock SHALL wait until at least `MEM0_BATCH_COOLDOWN` seconds
(default 10) have elapsed since the previous batch write completed. If more
time has already elapsed (e.g., the previous batch completed 15s ago and the
cooldown is 10s), no wait occurs. Single-record writes (`text=` with no
`messages`) SHALL NOT trigger or be subject to the cooldown. The completion
timestamp SHALL be recorded whenever a batch write reaches the upstream HTTP
layer, including when that request fails, and SHALL NOT be recorded when the
call is rejected before any HTTP request (for example, an oversized batch).

#### Scenario: Cooldown enforces idle time between batch writes
- **WHEN** a batch write completes at time T and another batch write arrives at T+2s
- **AND** `MEM0_BATCH_COOLDOWN` is 10
- **THEN** the second batch write waits until T+10s before sending its HTTP request

#### Scenario: Cooldown skipped when enough time has elapsed
- **WHEN** a batch write completed at time T and another batch write arrives at T+15s
- **AND** `MEM0_BATCH_COOLDOWN` is 10
- **THEN** the second batch write proceeds immediately without waiting

#### Scenario: Single-record writes bypass cooldown
- **WHEN** a batch write completes at time T and a single-record `add_memory` (text=, no messages) arrives at T+1s
- **THEN** the single-record write proceeds immediately without waiting

#### Scenario: Failed batch write still starts the cooldown
- **WHEN** a batch write reaches the upstream server and fails (for example HTTP 502 after retry)
- **THEN** the completion timestamp is recorded so the next batch write is still subject to the cooldown

#### Scenario: Rejected oversized batch does not start the cooldown
- **WHEN** an `add_memory` call is rejected by batch size validation before any HTTP request
- **THEN** the completion timestamp is unchanged and the next batch write's cooldown is unaffected

#### Scenario: Long batch write self-cooldowns
- **WHEN** a batch write takes 60s and another batch write arrives immediately after it completes
- **AND** `MEM0_BATCH_COOLDOWN` is 10
- **THEN** the second batch write proceeds immediately, because the cooldown is measured from the previous batch's completion and 60s of inference time already elapsed before it

#### Scenario: Operator tunes cooldown
- **WHEN** `MEM0_BATCH_COOLDOWN` is set to 30 in the environment
- **THEN** the system enforces a 30-second idle time between consecutive batch writes

### Requirement: Transient error retry

The system SHALL retry write HTTP requests that receive an HTTP 400 response
whose body contains the string `"terminated"` (matched case-insensitively; a
body that is absent, empty, or cannot be decoded as text SHALL be treated as
non-transient) exactly once, after waiting
`MEM0_RETRY_DELAY` seconds (default 10). The retry SHALL occur while holding
the write lock so that no other write can interfere. If the retry succeeds,
the system SHALL return the successful response. If the retry also fails, the
system SHALL return the error from the retry attempt. Non-400 errors and 400
errors without `"terminated"` in the body SHALL NOT be retried. The retry delay
SHALL use async sleep so the event loop remains responsive during the wait.

#### Scenario: Transient error recovers on retry
- **WHEN** the upstream server returns HTTP 400 with `{"error": "terminated"}` on the first attempt
- **AND** the retry after the delay succeeds with HTTP 200
- **THEN** the system returns the successful response

#### Scenario: Transient error persists after retry
- **WHEN** the upstream server returns HTTP 400 with `{"error": "terminated"}` on both the first and retry attempts
- **THEN** the system returns the error from the retry attempt

#### Scenario: Non-transient 400 is not retried
- **WHEN** the upstream server returns HTTP 400 with a body that does not contain `"terminated"`
- **THEN** the system returns the error immediately without retrying

#### Scenario: Unreadable 400 body is not retried
- **WHEN** the upstream server returns HTTP 400 with an empty, non-text, or undecodable body
- **THEN** the system treats the error as non-transient, returns it immediately without retrying, and does not raise an exception while inspecting the body

#### Scenario: Non-400 error is not retried
- **WHEN** the upstream server returns HTTP 500
- **THEN** the system returns the error immediately without retrying

#### Scenario: Retry does not block reads
- **WHEN** a write is retrying after a transient 400 and the retry delay is in progress
- **AND** a `search_memories` call arrives
- **THEN** the `search_memories` call executes immediately (the retry delay uses async sleep, not blocking sleep)

#### Scenario: Operator tunes retry delay
- **WHEN** `MEM0_RETRY_DELAY` is set to 30 in the environment
- **THEN** the system waits 30 seconds before retrying a transient 400 error
