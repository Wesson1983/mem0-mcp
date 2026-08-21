## 1. Schemas

- [ ] 1.1 Add `default_agent_id: Optional[str] = Field(None, description="Default agent_id injected into filters when unspecified.")` to `ConfigSchema` in `src/mem0_mcp_server/schemas.py`. Verify by importing `ConfigSchema` in a Python REPL and confirming `ConfigSchema.model_fields` contains `default_agent_id`.

## 2. Default resolution

- [ ] 2.1 In `src/mem0_mcp_server/server.py`, add `ENV_DEFAULT_AGENT_ID` near `ENV_DEFAULT_USER_ID` (line ~126): read `MEM0_DEFAULT_AGENT_ID`, trim, and treat whitespace-only as empty with a `logger.warning`. Verify by temporarily setting `MEM0_DEFAULT_AGENT_ID="  "` in a REPL and confirming the resolved value is empty and a warning is logged.
- [ ] 2.2 Extend `_resolve_settings` to return a 4-tuple `(api_key, default_user, default_agent, base_url)`. Resolve `default_agent` from session-config `default_agent_id` first, then override with `ENV_DEFAULT_AGENT_ID` if set (logging the same "Ignoring session-config ... override; env ... takes precedence" warning used for `default_user_id`). Verify by calling `_resolve_settings` with a fake `ctx.session_config={"default_agent_id":"X"}` and `MEM0_DEFAULT_AGENT_ID` unset → returns `"X"`; with `MEM0_DEFAULT_AGENT_ID="Y"` set → returns `"Y"` and logs the warning.
- [ ] 2.3 Update every `_resolve_settings` call site (`add_memory`, `get_memories`, `search_memories`, `delete_all_memories`, `delete_entities`, `get_memory`, `update_memory`, `history`, `list_entities`) to unpack the 4-tuple. For tools that do not use `default_agent` (read-one, history, list_entities, delete_entities), accept the value and ignore it. Verify by running `python -c "import mem0_mcp_server.server"` (or the equivalent import smoke test) with no errors.

## 3. Tool wiring

- [ ] 3.1 In `add_memory`, compute `user_id` using the caller-supplied `agent_id`/`run_id` FIRST (preserving the existing `user_id if user_id else (default_user if not (agent_id or run_id) else None)` rule), then set `agent_id = agent_id or default_agent` before constructing `AddMemoryArgs`. Verify with a unit-style check: `MEM0_DEFAULT_USER_ID=pavel`, `MEM0_DEFAULT_AGENT_ID=project-A`, call `add_memory(text="x")` → payload contains `user_id=pavel, agent_id=project-A`; call `add_memory(text="x", run_id="s1")` → payload contains `agent_id=project-A, run_id=s1`, NO `user_id`.
- [ ] 3.2 In `get_memories`, change `agent_id` handling to `agent_id=agent_id or default_agent` (parallel to the existing `user_id or default_user`). Verify: `MEM0_DEFAULT_AGENT_ID=project-A`, call `get_memories()` → params contain `agent_id=project-A`; call `get_memories(agent_id="B")` → params contain `agent_id=B`.
- [ ] 3.3 In `delete_all_memories`, change `agent_id` handling to `agent_id=agent_id or default_agent`. Verify: `MEM0_DEFAULT_AGENT_ID=project-A`, call `delete_all_memories()` → params contain `agent_id=project-A`.
- [ ] 3.4 Rename `_with_default_user` to `_with_default_filters` and extend it to also inject `agent_id` into the filters dict when the key is absent and `default_agent` is non-empty. Update the `search_memories` call site to pass `default_agent` and use the new name. Verify: `MEM0_DEFAULT_AGENT_ID=project-A`, `search_memories(query="q", filters={"user_id":"pavel"})` → body contains `filters={"user_id":"pavel","agent_id":"project-A"}`; `search_memories(query="q", filters={"user_id":"pavel","agent_id":"B"})` → caller's `"B"` preserved.

## 4. Docs

- [ ] 4.1 Add a bullet for `MEM0_DEFAULT_AGENT_ID` to the "Environment variables" section of `AGENTS.md`, placed immediately after the `MEM0_DEFAULT_USER_ID` bullet. Note that explicit caller `agent_id` wins, that unset = no agent scope, and that `list_entities` is unaffected. Verify by reading the updated `AGENTS.md` section.

## 5. End-to-end verification

- [ ] 5.1 With `MEM0_DEFAULT_AGENT_ID` unset, re-run the existing JSON-RPC handshake from `AGENTS.md` ("Verification" section) and confirm `tools/list` still returns 10 tools and `list_entities` still returns non-401. Confirms no regression when the new env is absent.
- [ ] 5.2 Set `MEM0_DEFAULT_AGENT_ID=verify-proj` in `.env.local`, restart the container, and call `add_memory(text="probe")` then `get_memories()` over the MCP protocol. Confirm both the stored memory and the list result carry `agent_id=verify-proj`. Then call `get_memories(agent_id="other")` and confirm the probe memory is NOT returned (proves explicit wins and the default actually scopes). Remove the probe memory afterward.
