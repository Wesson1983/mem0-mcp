## Purpose

Server-side resolution of default scope identifiers (`user_id`, `agent_id`) from
environment variables and session config, applied to memory write and read tools
when the caller does not supply them explicitly. Enables a single operator to
partition memories across multiple projects and an always-on LLM from one mem0
instance without every caller having to pass `agent_id` on each tool call.

## ADDED Requirements

### Requirement: Default agent_id from environment

The server SHALL read `MEM0_DEFAULT_AGENT_ID` from the environment at startup.
When unset, the default agent scope SHALL be empty (no agent discriminator).
When set, the value SHALL be trimmed of surrounding whitespace; an empty-after-
trim value SHALL be treated as unset with a warning logged.

#### Scenario: Env var unset
- **WHEN** `MEM0_DEFAULT_AGENT_ID` is not present in the environment
- **THEN** the resolved default agent scope is empty and no `agent_id` is
  injected into any tool call by the default-resolution mechanism

#### Scenario: Env var set to a non-empty value
- **WHEN** `MEM0_DEFAULT_AGENT_ID` is set to `"  mem0-mcp  "`
- **THEN** the resolved default agent scope is `"mem0-mcp"` (trimmed)

#### Scenario: Env var set to whitespace only
- **WHEN** `MEM0_DEFAULT_AGENT_ID` is set to `"   "`
- **THEN** the server logs a warning and treats the default agent scope as
  empty, identical to the unset case

### Requirement: Explicit caller agent_id wins over default

For every memory tool that accepts `agent_id`, an `agent_id` value supplied by
the caller on that tool invocation SHALL take precedence over the resolved
default agent scope. The default SHALL be injected only when the caller did not
supply `agent_id` for that call.

#### Scenario: Caller passes agent_id on add_memory
- **WHEN** `MEM0_DEFAULT_AGENT_ID=project-A` and a caller invokes `add_memory`
  with `agent_id="project-B"`
- **THEN** the memory is stored with `agent_id="project-B"`

#### Scenario: Caller omits agent_id on add_memory
- **WHEN** `MEM0_DEFAULT_AGENT_ID=project-A` and a caller invokes `add_memory`
  without `agent_id`
- **THEN** the memory is stored with `agent_id="project-A"`

### Requirement: Default agent_id applied to writes

`add_memory` SHALL apply the resolved default agent scope when the caller omits
`agent_id`. The existing rule that `user_id` is auto-filled only when neither
`agent_id` nor `run_id` is supplied by the caller SHALL be preserved, evaluated
against the caller-supplied values before default injection.

#### Scenario: Default agent_id used, user_id still auto-filled
- **WHEN** `MEM0_DEFAULT_USER_ID=pavel` and `MEM0_DEFAULT_AGENT_ID=project-A`
  and a caller invokes `add_memory` with only `text`
- **THEN** the memory is stored with `user_id="pavel"` and
  `agent_id="project-A"`

#### Scenario: Caller supplies run_id only
- **WHEN** `MEM0_DEFAULT_USER_ID=pavel` and `MEM0_DEFAULT_AGENT_ID=project-A`
  and a caller invokes `add_memory` with `run_id="session-1"` only
- **THEN** the memory is stored with `agent_id="project-A"` and
  `run_id="session-1"` and `user_id` is NOT auto-filled (the existing rule
  suppresses user_id when any of agent_id/run_id is present, and agent_id is
  present after default injection)

### Requirement: Default agent_id applied to reads and deletes

`get_memories`, `search_memories`, and `delete_all_memories` SHALL apply the
resolved default agent scope when the caller omits `agent_id`. For
`search_memories`, the default SHALL be injected into the `filters` dict under
the `agent_id` key when that key is absent.

#### Scenario: get_memories with no agent_id
- **WHEN** `MEM0_DEFAULT_AGENT_ID=project-A` and a caller invokes
  `get_memories` without `agent_id`
- **THEN** the request to the OSS server includes `agent_id=project-A`

#### Scenario: search_memories with filters lacking agent_id
- **WHEN** `MEM0_DEFAULT_AGENT_ID=project-A` and a caller invokes
  `search_memories` with `filters={"user_id":"pavel"}` and no top-level
  `agent_id` parameter
- **THEN** the search body sent to the OSS server contains
  `filters={"user_id":"pavel","agent_id":"project-A"}`

#### Scenario: search_memories with filters already containing agent_id
- **WHEN** `MEM0_DEFAULT_AGENT_ID=project-A` and a caller invokes
  `search_memories` with `filters={"user_id":"pavel","agent_id":"project-B"}`
- **THEN** the search body sent to the OSS server contains
  `filters={"user_id":"pavel","agent_id":"project-B"}` (caller value preserved)

#### Scenario: delete_all_memories with no agent_id
- **WHEN** `MEM0_DEFAULT_AGENT_ID=project-A` and a caller invokes
  `delete_all_memories` without `agent_id`
- **THEN** the delete request to the OSS server includes
  `agent_id=project-A`

### Requirement: Session-config default_agent_id supported

The session-config schema SHALL accept an optional `default_agent_id` field
mirroring the existing `default_user_id` field. The environment variable
`MEM0_DEFAULT_AGENT_ID` SHALL take precedence over session-config
`default_agent_id` with a warning logged on conflict, matching the precedence
this change establishes for `default_user_id` (see the "Default user_id
precedence aligned to env-wins" requirement below).

#### Scenario: Env var overrides session config
- **WHEN** `MEM0_DEFAULT_AGENT_ID=project-A` is set and session config supplies
  `default_agent_id="project-B"`
- **THEN** the resolved default agent scope is `"project-A"` and a warning is
  logged that the session-config value is being ignored

#### Scenario: Session config used when env var unset
- **WHEN** `MEM0_DEFAULT_AGENT_ID` is unset and session config supplies
  `default_agent_id="project-B"`
- **THEN** the resolved default agent scope is `"project-B"`

### Requirement: Default user_id precedence aligned to env-wins

The environment variable `MEM0_DEFAULT_USER_ID` SHALL take precedence over
session-config `default_user_id` with a warning logged on conflict, matching
the precedence rule applied to `default_agent_id`, `mem0_api_key`, and
`base_url`. Prior to this change, `default_user_id` resolved session-wins with
no warning; this change aligns it to the shared env-wins-with-warning rule so
all four resolved fields have one precedence.

Because `MEM0_DEFAULT_USER_ID` carries a built-in default of `mem0-mcp`, the
env value is never empty. A session-config `default_user_id` is therefore in
practice always overridden (with a warning); operators who want a non-default
user MUST set `MEM0_DEFAULT_USER_ID` in the environment.

#### Scenario: Env var overrides session config
- **WHEN** `MEM0_DEFAULT_USER_ID=pavel` is set and session config supplies
  `default_user_id="guest"`
- **THEN** the resolved default user is `"pavel"` and a warning is logged that
  the session-config value is being ignored

#### Scenario: Env var at built-in default still overrides session config
- **WHEN** `MEM0_DEFAULT_USER_ID` is unset (so resolves to the built-in default
  `mem0-mcp`) and session config supplies `default_user_id="guest"`
- **THEN** the resolved default user is `"mem0-mcp"` and a warning is logged
  that the session-config value is being ignored

#### Scenario: Env var unset, no session config
- **WHEN** `MEM0_DEFAULT_USER_ID` is unset and session config supplies no
  `default_user_id`
- **THEN** the resolved default user is `"mem0-mcp"` (the built-in default) and
  no warning is logged

### Requirement: Backward compatibility when default agent_id unset

When `MEM0_DEFAULT_AGENT_ID` is unset and session config supplies no
`default_agent_id`, no `agent_id` SHALL be injected into any tool call by the
default-resolution mechanism — agent-scoping behavior is identical to prior to
this change. (The `default_user_id` precedence change is a separate,
documented exception; see the "Default user_id precedence aligned to
env-wins" requirement.)

#### Scenario: No default agent_id configured
- **WHEN** neither `MEM0_DEFAULT_AGENT_ID` nor session-config
  `default_agent_id` is set
- **THEN** `add_memory`, `get_memories`, `search_memories`, and
  `delete_all_memories` inject no `agent_id`, matching pre-change agent-scoping
  behavior
