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
- No graph memory; `MEM0_ENABLE_GRAPH_DEFAULT` is intentionally ignored.
- `GET /memories` takes flat query params, not AND/OR/NOT filter trees.
- Entity deletion is `DELETE /entities/{type}/{id}`.
- Extra write options: `expiration_date`, `infer`, `memory_type`, `prompt`.

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
