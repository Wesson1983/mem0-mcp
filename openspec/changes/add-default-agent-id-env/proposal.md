## Why

The MCP server defaults only `user_id` (via `MEM0_DEFAULT_USER_ID`). A single
operator running mem0 across multiple repos and an always-on LLM has no way to
discriminate memories per project without passing `agent_id` on every tool call.
Today every unscoped write lands in the same `user_id` bucket, so cross-project
filtering by `agent_id` is only possible if callers never forget to pass it —
which is exactly the failure mode this change removes. Adding a per-instance
`MEM0_DEFAULT_AGENT_ID` env lets each repo's `.env.local` (or MCP config) pin a
project discriminator once, and the server injects it on every write and read
where the caller did not pass one explicitly.

## What Changes

- Add `MEM0_DEFAULT_AGENT_ID` environment variable, read once at startup like
  `MEM0_DEFAULT_USER_ID`, defaulting to unset (no agent scope).
- `_resolve_settings` returns a fourth value, `default_agent`, sourced from env
  (env wins over session-config `default_agent_id` with a warning on conflict).
  `default_user_id` is also aligned to the same env-wins-with-warning rule
  (previously session-wins, no warning), so all four resolved fields
  (`api_key`, `default_user`, `default_agent`, `base_url`) share one
  precedence.
- `add_memory`: when the caller does not pass `agent_id` and `default_agent` is
  set, inject `default_agent`. Explicit `agent_id` always wins. The existing
  "default `user_id` only when no agent_id and no run_id" rule is preserved.
- `get_memories` and `delete_all_memories`: when the caller does not pass
  `agent_id` and `default_agent` is set, inject `default_agent`. Explicit wins.
- `search_memories`: the existing `_with_default_user` helper is extended (or a
  parallel helper added) so `agent_id` is injected into `filters` when absent
  and `default_agent` is set.
- `AGENTS.md` documents the new env var alongside `MEM0_DEFAULT_USER_ID`.

## Capabilities

### New Capabilities
- `scope-defaults`: Server-side resolution of default scope identifiers
  (`user_id`, `agent_id`) from environment and session config, applied to memory
  write and read tools when the caller omits them.

### Modified Capabilities
<!-- None. No prior specs exist in this repo. -->

## Impact

- **Code**: `src/mem0_mcp_server/server.py` — `_resolve_settings` signature,
  `add_memory`, `get_memories`, `delete_all_memories`, `search_memories`, and
  the `_with_default_user` helper. `src/mem0_mcp_server/schemas.py` if
  `ConfigSchema` exposes session-config fields (add `default_agent_id`).
- **Env**: one new optional variable, `MEM0_DEFAULT_AGENT_ID`. No new required
  variables; `MEM0_API_KEY` remains the only hard requirement.
- **Behavior**: mostly backward compatible. With `MEM0_DEFAULT_AGENT_ID` unset,
  agent-scoping behavior is identical to today. With it set, unscoped calls
  gain an `agent_id` filter — callers that already pass `agent_id` are
  unaffected. One exception: `default_user_id` precedence changes from
  session-wins to env-wins-with-warning (see design.md Risks), so operators
  relying on session-config `default_user_id` without setting
  `MEM0_DEFAULT_USER_ID` must now set the env var.
- **Docs**: `AGENTS.md` "Environment variables" section gains one bullet and
  the `MEM0_DEFAULT_USER_ID` bullet is updated to note env-wins-with-warning.
- **No upstream mem0 fork required.** This is MCP-layer only; the OSS REST
  server already accepts `agent_id` on `/memories`, `/search`, and
  `DELETE /memories`.
