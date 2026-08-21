# mem0-mcp-server (self-hosted OSS edition)

This fork targets a **self-hosted mem0 OSS REST server**, not Mem0 Cloud.

## Why this diverges from upstream

Upstream `mem0-mcp-server` calls the mem0 Python `MemoryClient`, which hardcodes
platform-style `/v1/...` paths. The OSS REST server exposes bare paths instead
(`/memories`, `/search`, `/entities`), so the SDK cannot talk to it
(mem0 issue #4777). Verified locally:

- `GET /v1/memories` -> 404
- `GET /memories` -> 401 (exists, needs auth)

`src/mem0_mcp_server/server.py` therefore calls the REST API directly via
`requests` with `X-API-Key` auth. Do **not** reintroduce `MemoryClient`.

## OSS vs Cloud API differences baked into this code

- Auth: `X-API-Key` header (or bearer JWT). Keys are minted in the dashboard.
- No `app_id` — scopes are `user_id`, `agent_id`, `run_id` only.
- No graph memory; `MEM0_ENABLE_GRAPH_DEFAULT` is intentionally ignored. See
  "Why graph memory is not supported" below.
- `GET /memories` takes flat query params, not AND/OR/NOT filter trees.
- Entity deletion is `DELETE /entities/{type}/{id}`.
- Extra write options: `expiration_date`, `infer`, `memory_type`, `prompt`.

## Why graph memory is not supported

Two independent reasons, either of which is sufficient on its own.

**Upstream: graph memory is removed from mem0 OSS as of v3.** Verified against
`main` on 2026-08-21:

- `mem0/memory/main.py` — `findstr graph` returns zero matches; `Memory` has no
  graph code path.
- `mem0/configs/base.py` — `MemoryConfig` has no `graph_store` and no
  `enable_graph` field.
- `server/main.py` — no `/graphs` or graph-specific routes; the full route
  surface is `/configure`, `/memories` (CRUD), `/search`, `/reset`, plus the
  `auth`/`api_keys`/`entities`/`requests` routers. The `entities` router is
  entity-scoped memory (user/agent/run buckets), not graph entities.
- `docs/migration/oss-v2-to-v3` states it directly: "Graph memory is removed
  from OSS. It's a built-in, always-on Mem0 Platform feature."

On v2 OSS, graph worked via external store (Neo4j/Memgraph/Kuzu/AGE/Neptune)
configured through `POST /configure` with a `graph_store` block; `search`
returned a `relations` array automatically. On v3 OSS there is nothing to
enable — the core no longer has the code. `MEM0_ENABLE_GRAPH_DEFAULT` was a
cloud-edition MCP flag and was never meaningful on OSS (graph on OSS v2 was
server config, not a per-call flag). Reintroducing graph would require pinning
the OSS server to v2, moving to Mem0 Platform, or building a parallel graph
store outside mem0 — none of which fit this repo's self-hosted OSS stance.

**Use cases: the consumers of this MCP server don't need it.** The two target
workloads are:

1. **Software factory** — memory passed between pipeline steps. This is
   scope-filtered recall, not relational. `run_id` (per execution) +
   `agent_id` (per factory, made automatic by the `add-default-agent-id-env`
   change) + `metadata={"step": ...}` covers it. Graph would only help for
   cross-run relational queries ("which components depend on the module that
   just changed, across all past runs"), which `metadata` filters + multiple
   searches handle adequately.
2. **Local personal-assistant LLM** — "remember / recall what I said" is
   user-scoped vector memory, already the default via `MEM0_DEFAULT_USER_ID`.
   Graph's entity-centric and multi-hop recall is a marginal benefit here, not
   a need.

Revisit only if a concrete relational query emerges that `metadata` + filtered
`search`/`get_memories` cannot handle. At that point the gating decision is
which mem0 OSS version is deployed (v2 pin vs. Platform vs. parallel store),
not what the MCP layer does.

## Dependency constraint

`mcp[cli]` is pinned `<2.0.0`. MCP 2.x removed `mcp.server.fastmcp`
(`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`), which this
server's `FastMCP` usage depends on.

## Environment variables

- `MEM0_API_KEY` (required) — OSS API key from the dashboard.
- `MEM0_BASE_URL` — OSS API base. Default `http://localhost:8888`; the
  Dockerfile defaults to `http://host.docker.internal:8888` so the container
  can reach the host. On Linux Docker, `host.docker.internal` does not resolve
  by default — override it or add `--add-host`.
- `MEM0_DEFAULT_USER_ID` — default scope (defaults to `mem0-mcp`).
- `MEM0_HTTP_TIMEOUT` — seconds, default `300`. Self-hosted writes run LLM fact
  extraction + embeddings; with local models (LM Studio / Ollama) a single
  `add_memory` measured **~48s**. A 30s timeout is too low.

## Build and run

```powershell
docker build -t mem0-mcp-oss:latest .
docker run -d --name mem0-mcp-oss --restart unless-stopped `
  -p 8765:8081 --env-file .env.local mem0-mcp-oss:latest
```

Secrets live in `.env.local` (gitignored). The tracked `.env` holds placeholders
only — never put a real key there, it is a tracked file.

## Devin CLI wiring

`.devin/mcp_config.local.json` (gitignored) points at the container:

```json
{ "mcpServers": { "mem0-oss": { "url": "http://localhost:8765/mcp", "transport": "http" } } }
```

## Verification

There is no test suite. Verify over the wire with a JSON-RPC handshake
(Streamable HTTP requires `Accept: application/json, text/event-stream`):

1. `POST /mcp` `initialize` -> capture the `mcp-session-id` response header
2. `POST /mcp` `notifications/initialized` with that header
3. `POST /mcp` `tools/list` -> expect 10 tools
4. `tools/call` `list_entities` -> proves auth works (non-401)

PowerShell notes: `curl` is aliased to `Invoke-WebRequest` — use `curl.exe`.
PowerShell also mangles inline JSON quoting, so drive these checks from Python.
