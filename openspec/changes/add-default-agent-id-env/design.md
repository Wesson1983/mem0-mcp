## Context

See `proposal.md` for motivation. The relevant current-state facts:

- `_resolve_settings(ctx)` returns `(api_key, default_user, base_url)` from
  env-or-session-config, with env winning over session config for both
  `mem0_api_key` and `base_url`, and a warning logged on conflict
  (`src/mem0_mcp_server/server.py:156-179`).
- `ENV_DEFAULT_USER_ID = os.getenv("MEM0_DEFAULT_USER_ID", "mem0-mcp")`
  (`server.py:126`).
- `add_memory` derives `user_id` as
  `user_id if user_id else (default_user if not (agent_id or run_id) else None)`
  (`server.py:395`) — i.e. the default user is only injected when the caller
  supplied neither `agent_id` nor `run_id`.
- `get_memories` and `delete_all_memories` use
  `user_id=user_id or default_user` and pass `agent_id`/`run_id` through
  unchanged (`server.py:494`, `server.py:520`).
- `search_memories` uses `_with_default_user(filters, default_user)` which
  injects `user_id` into the filters dict only when absent (`server.py:295-300`,
  used at `server.py:455`).
- `ConfigSchema` (`schemas.py:27-36`) exposes session-config fields
  `mem0_api_key`, `default_user_id`, `base_url`.

The mem0 OSS REST server already accepts `agent_id` on `POST /memories`,
`GET /memories`, `POST /search` (inside `filters`), and `DELETE /memories`, so
no upstream change is required.

## Goals / Non-Goals

**Goals:**
- Add `MEM0_DEFAULT_AGENT_ID` env support mirroring `MEM0_DEFAULT_USER_ID`.
- Apply the default `agent_id` consistently across `add_memory`, `get_memories`,
  `search_memories`, `delete_all_memories` when the caller omits it.
- Preserve the existing "explicit caller value wins" and "env wins over session
  config" precedence patterns already established for `user_id`.
- Stay backward compatible: unset env + unset session config = identical
  behavior to today.

**Non-Goals:**
- No `MEM0_DEFAULT_RUN_ID`. The user explicitly scoped this to `agent_id` only.
  Run-id remains caller-supplied.
- No `app_id` support. OSS has no `app_id`; `user_id` plays that role.
- No graph memory, no AND/OR/NOT filter-tree work on `GET /memories`. Those are
  separate upstream concerns, out of scope here.
- No change to the OSS REST server or the mem0 core SDK.
- No new MCP tools. The existing four tools gain default-resolution behavior.

## Decisions

### D1: Resolve `default_agent` inside `_resolve_settings`

Extend `_resolve_settings` to return a 4-tuple
`(api_key, default_user, default_agent, base_url)`. Resolution mirrors
`default_user`: env `MEM0_DEFAULT_AGENT_ID` wins over session-config
`default_agent_id`, with a warning on conflict; session config used when env is
unset; empty when neither is set.

**Why:** centralizes all default resolution in one place, so every tool gets
the same value via the same precedence. Avoids four separate
`os.getenv` calls and four separate conflict-warning code paths.

**Alternative considered:** resolve `default_agent` inline in each tool. Rejected
— duplicates the env-vs-session-config precedence four times and risks drift.

### D2: Trim and validate the env value

Apply the same trim-and-treat-empty-as-unset rule the mem0 core SDK applies to
entity IDs (`_validate_and_trim_entity_id` in `mem0/memory/main.py`). A
whitespace-only `MEM0_DEFAULT_AGENT_ID` logs a warning and resolves to empty,
rather than being sent to the OSS server as a literal `"   "` (which the core
SDK would reject with `ValueError`).

**Why:** the OSS server delegates entity-id validation to the core SDK, which
rejects whitespace-only values. Catching this at MCP startup avoids a confusing
runtime 400 on every tool call.

**Alternative considered:** pass through unchanged and let the OSS server
reject. Rejected — the failure surfaces per-call instead of once at startup,
and the error message from the SDK is generic.

### D3: `add_memory` injection point

Inject `default_agent` into the `agent_id` slot of `AddMemoryArgs` after the
caller's value is known, using the same fallback pattern as `user_id`:

```
agent_id = agent_id if agent_id else default_agent
```

The existing `user_id` defaulting rule
(`user_id if user_id else (default_user if not (agent_id or run_id) else None)`)
MUST be evaluated against the caller-supplied `agent_id`/`run_id` BEFORE
`default_agent` is injected — otherwise injecting `default_agent` would suppress
the `user_id` default in a case where the caller supplied neither, changing
behavior for users who set `MEM0_DEFAULT_USER_ID` but not
`MEM0_DEFAULT_AGENT_ID`.

Concretely: compute `user_id` first using the caller's `agent_id`/`run_id`,
then compute `agent_id = agent_id or default_agent`.

**Why:** preserves the existing user-id defaulting semantics exactly for users
who do not adopt `MEM0_DEFAULT_AGENT_ID`.

**Alternative considered:** inject `default_agent` before evaluating the
`user_id` rule. Rejected — it would change `user_id` defaulting behavior for
existing users who set only `MEM0_DEFAULT_USER_ID`, breaking backward
compatibility.

### D4: Read/delete injection — `agent_id or default_agent`

For `get_memories` and `delete_all_memories`, replace
`user_id=user_id or default_user` with the same pattern for `agent_id`:
`agent_id=agent_id or default_agent`. Pass through to `GetMemoriesArgs` /
`DeleteAllArgs` as today.

**Why:** symmetric with `user_id` handling; minimal diff.

### D5: `search_memories` — extend `_with_default_user` or add a parallel helper

Extend the existing `_with_default_user` helper to also inject `agent_id` when
absent and `default_agent` is set. Rename to `_with_default_filters` to reflect
that it now handles both fields, and update the one call site in
`search_memories`.

The helper MUST NOT overwrite an `agent_id` already present in the caller's
`filters` dict (covers the case where the caller passes
`filters={"agent_id":"X"}`).

**Why:** one helper, one call site, one precedence rule. Renaming makes the
broader responsibility legible at the call site.

**Alternative considered:** add a separate `_with_default_agent` helper and
chain the calls. Rejected — two helpers for one filters dict invites ordering
bugs and is more code for no benefit.

### D6: `ConfigSchema` gains `default_agent_id`

Add `default_agent_id: Optional[str] = Field(None, ...)` to `ConfigSchema` in
`schemas.py`, mirroring `default_user_id`. This is what Smithery/HTTP session
config introspects; without it, session-config `default_agent_id` would be
silently dropped.

**Why:** required for the session-config path in the spec's "Session-config
default_agent_id supported" requirement.

## Risks / Trade-offs

- **[Risk] Existing users with `MEM0_DEFAULT_USER_ID` set who adopt
  `MEM0_DEFAULT_AGENT_ID` will see `user_id` stop being auto-filled on writes
  where they previously relied on it.**
  → Mitigation: this is the intended new behavior (agent_id now present, so the
  user-id rule correctly suppresses). Documented in the spec scenario "Caller
  supplies run_id only" and in AGENTS.md. Users who want the old behavior simply
  do not set `MEM0_DEFAULT_AGENT_ID`.

- **[Risk] A user sets `MEM0_DEFAULT_AGENT_ID` expecting it to also filter
  `list_entities`, which takes no scope.**
  → Mitigation: `list_entities` returns all entities server-wide and takes no
  scope params today; no change. Document in AGENTS.md that the default does not
  affect `list_entities`.

- **[Risk] Trimming silently changes a value the user intended to contain
  leading/trailing spaces.**
  → Mitigation: mem0 core rejects internal/leading/trailing whitespace in
  entity IDs anyway, so trimming at the MCP layer surfaces the issue earlier
  and matches the core's own normalization. No real loss.

- **[Trade-off] Renaming `_with_default_user` → `_with_default_filters` is a
  private-symbol rename; no external API impact but touches one call site.**
  → Acceptable; the alternative (keeping the old name) is misleading.

## Migration Plan

1. Ship the change. No data migration; this is default-resolution behavior only.
2. Operators who want per-project scoping set `MEM0_DEFAULT_AGENT_ID` in each
   repo's `.env.local` (or MCP config env) to a stable project identifier.
3. Operators who do nothing continue with identical behavior.

**Rollback:** unset `MEM0_DEFAULT_AGENT_ID` and remove `default_agent_id` from
any session config. Behavior reverts to pre-change.

## Open Questions

None. All material decisions were resolved with the user before proposal
capture (scope: `agent_id` only; precedence: explicit wins; output: proposal
not implementation).
