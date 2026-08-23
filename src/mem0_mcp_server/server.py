"""MCP server that exposes the Mem0 OSS REST API as MCP tools.

This edition talks directly to a self-hosted mem0 OSS REST server
(https://github.com/mem0ai/mem0/tree/main/server) using bare paths
(/memories, /search, /entities) and X-API-Key auth. The upstream
mem0 Python MemoryClient is NOT used because it hardcodes /v1/...
cloud paths that do not exist on the OSS server (mem0 issue #4777).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.parse
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, cast

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError

try:  # Support both package and script runs.
    from .schemas import (
        AddMemoryArgs,
        DeleteAllArgs,
        DeleteEntitiesArgs,
        GetMemoriesArgs,
        SearchMemoriesArgs,
        ToolMessage,
        UpdateMemoryArgs,
    )
except ImportError:  # pragma: no cover - fallback for script execution
    from schemas import (  # type: ignore
        AddMemoryArgs,
        DeleteAllArgs,
        DeleteEntitiesArgs,
        GetMemoriesArgs,
        SearchMemoriesArgs,
        ToolMessage,
        UpdateMemoryArgs,
    )

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("mem0_mcp_server")

# --- Helpers ---------------------------------------------------------------

# `host.docker.internal` resolves to the Docker host from inside a container;
# treating it as local avoids forcing HTTPS for the standard Docker Desktop setup.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def _validate_base_url(url: str) -> str:
    """Validate the mem0 OSS base URL scheme and HTTPS requirement."""
    url = url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"MEM0_BASE_URL must use http:// or https://, got: {url}")
    host = urllib.parse.urlparse(url).hostname
    if host not in _LOCAL_HOSTS and not url.startswith("https://"):
        raise ValueError(f"MEM0_BASE_URL must use HTTPS for non-local hosts, got: {url}")
    return url


_REDACT_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key["\']?\s*[:=]\s*)["\']?[A-Za-z0-9_\-]{20,}'),
    re.compile(r'(?i)(token["\']?\s*[:=]\s*)["\']?[A-Za-z0-9_\-]{20,}'),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{20,}"),
    re.compile(r'(?i)(authorization["\']?\s*[:=]\s*)["\']?[A-Za-z0-9_\-\.=]{20,}'),
]


def _redact(text: str, limit: int = 500) -> str:
    """Redact sensitive values in `text` and truncate to `limit` chars."""
    for pat in _REDACT_PATTERNS:
        text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text[:limit]


_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_memory_id(memory_id: str) -> str:
    """Reject memory IDs that could break the URL path."""
    if not memory_id or not _MEMORY_ID_RE.match(memory_id):
        raise ValueError(f"Invalid memory_id format: {memory_id!r}")
    return memory_id


def _error(code: str, detail: str, status: int | None = None) -> dict[str, Any]:
    """Build a standardized error dict returned by tools and `_call`."""
    err: dict[str, Any] = {"error": code, "detail": detail}
    if status is not None:
        err["status"] = status
    return err


# --- Configuration ---------------------------------------------------------

ENV_API_KEY = os.getenv("MEM0_API_KEY")
ENV_BASE_URL = _validate_base_url(os.getenv("MEM0_BASE_URL", "http://localhost:8888"))
ENV_DEFAULT_USER_ID = os.getenv("MEM0_DEFAULT_USER_ID", "mem0-mcp")
# Default agent scope mirrors ENV_DEFAULT_USER_ID: read once at startup, trim, and
# treat whitespace-only as unset (the mem0 core SDK rejects whitespace entity IDs).
_raw_default_agent_id = os.getenv("MEM0_DEFAULT_AGENT_ID")
if _raw_default_agent_id is not None:
    _raw_default_agent_id = _raw_default_agent_id.strip()
    if not _raw_default_agent_id:
        logger.warning(
            "MEM0_DEFAULT_AGENT_ID is set to a whitespace-only value; treating as unset."
        )
# Resolve to None (not "") when unset: callers pass `agent_id or default_agent`
# into Pydantic models serialized with exclude_none=True. An empty string would
# survive exclude_none and leak "agent_id": "" into the payload, violating the
# spec's "no agent_id injected when default unset" guarantee. None is excluded.
ENV_DEFAULT_AGENT_ID: str | None = _raw_default_agent_id or None
# Note: the OSS server has no graph-memory endpoints, so MEM0_ENABLE_GRAPH_DEFAULT
# (supported by the cloud edition of this server) is intentionally not honoured here.

def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back to `default` when unset or malformed."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


# Self-hosted mem0 runs LLM fact-extraction + embeddings on every write, which can
# take minutes on local models (Ollama / LM Studio). Default generously.
_WRITE_TIMEOUT = _int_env("MEM0_HTTP_TIMEOUT", 300)
_READ_TIMEOUT = _int_env("MEM0_HTTP_READ_TIMEOUT", 60)


def _config_value(source: Any, field: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(field)
    return getattr(source, field, None) if hasattr(source, field) else None


def _resolve_settings(ctx: Context[Any, Any] | None) -> tuple[str, str, str | None, str]:
    """Return (api_key, default_user, default_agent, base_url) from session config or env.

    For all four fields, env wins over session config with a warning on conflict
    (matching the established api_key/base_url pattern). default_agent is None
    when no default is configured (env unset and no session config), so callers'
    `agent_id or default_agent` yields None and is dropped by exclude_none=True
    — preserving pre-change behavior of not injecting agent_id. default_user
    always resolves to at least ENV_DEFAULT_USER_ID, whose built-in default is
    "mem0-mcp"; because that env value is never empty, a session-config
    default_user_id is always overridden (with a warning) — operators must set
    MEM0_DEFAULT_USER_ID to change the default user.
    """
    session_config = getattr(ctx, "session_config", None)
    session_api_key = _config_value(session_config, "mem0_api_key")
    if session_api_key and ENV_API_KEY:
        logger.warning(
            "Ignoring session-config mem0_api_key override; env MEM0_API_KEY takes precedence."
        )
        session_api_key = None
    api_key = session_api_key or ENV_API_KEY
    if not api_key:
        raise RuntimeError(
            "MEM0_API_KEY is required (via session config or environment) "
            "to run the Mem0 MCP server."
        )
    session_default_user = _config_value(session_config, "default_user_id")
    if session_default_user and ENV_DEFAULT_USER_ID:
        logger.warning(
            "Ignoring session-config default_user_id override; "
            "env MEM0_DEFAULT_USER_ID takes precedence."
        )
        session_default_user = None
    default_user = session_default_user or ENV_DEFAULT_USER_ID
    session_default_agent = _config_value(session_config, "default_agent_id")
    if session_default_agent and ENV_DEFAULT_AGENT_ID:
        logger.warning(
            "Ignoring session-config default_agent_id override; "
            "env MEM0_DEFAULT_AGENT_ID takes precedence."
        )
        session_default_agent = None
    default_agent = session_default_agent or ENV_DEFAULT_AGENT_ID
    session_base_url = _config_value(session_config, "base_url")
    if session_base_url and ENV_BASE_URL:
        logger.warning(
            "Ignoring session-config base_url override; env MEM0_BASE_URL takes precedence."
        )
        session_base_url = None
    base_url = _validate_base_url(session_base_url or ENV_BASE_URL)
    return api_key, default_user, default_agent, base_url


# --- HTTP helper -----------------------------------------------------------

class Mem0OSSClient:
    """Thin REST client for the mem0 OSS server."""

    def __init__(self, base_url: str, api_key: str):
        self._base = base_url
        self._session = requests.Session()
        self._session.headers.update(
            {"X-API-Key": api_key, "Content-Type": "application/json"}
        )

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        if timeout is None:
            timeout = _WRITE_TIMEOUT if method in ("POST", "PUT", "PATCH") else _READ_TIMEOUT
        try:
            resp = self._session.request(
                method,
                f"{self._base}{path}",
                params=params,
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            clear_client_cache()
            logger.error("HTTP call failed: %s %s -> %s", method, path, exc)
            return _error("http_request_failed", str(exc))

        if resp.status_code >= 400:
            logger.error(
                "HTTP %s %s -> %s: %s",
                method,
                path,
                resp.status_code,
                _redact(resp.text, 500),
            )
            return _error(f"http_{resp.status_code}", _redact(resp.text, 1000), status=resp.status_code)
        try:
            return cast(dict[str, Any], resp.json())
        except ValueError:
            return {"message": _redact(resp.text, 1000)}

    # Memory CRUD
    def add(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/memories", json_body=body)

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/search", json_body=body)

    def list_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._call("GET", "/memories", params=params)

    def get(self, memory_id: str) -> dict[str, Any]:
        _validate_memory_id(memory_id)
        return self._call("GET", f"/memories/{memory_id}")

    def update(self, memory_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _validate_memory_id(memory_id)
        return self._call("PUT", f"/memories/{memory_id}", json_body=body)

    def delete(self, memory_id: str) -> dict[str, Any]:
        _validate_memory_id(memory_id)
        return self._call("DELETE", f"/memories/{memory_id}")

    def delete_all(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._call("DELETE", "/memories", params=params, timeout=_WRITE_TIMEOUT)

    def history(self, memory_id: str) -> dict[str, Any]:
        _validate_memory_id(memory_id)
        return self._call("GET", f"/memories/{memory_id}/history")

    # Entities
    def list_entities(self) -> dict[str, Any]:
        return self._call("GET", "/entities")

    def delete_entity(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        _validate_memory_id(entity_type)
        _validate_memory_id(entity_id)
        return self._call("DELETE", f"/entities/{entity_type}/{entity_id}")


_CLIENT_CACHE: dict[tuple[str, str], Mem0OSSClient] = {}
_CLIENT_CACHE_MAX = 32


def _client(base_url: str, api_key: str) -> Mem0OSSClient:
    key = (base_url, hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16])
    cached = _CLIENT_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_CLIENT_CACHE) >= _CLIENT_CACHE_MAX:
        _CLIENT_CACHE.pop(next(iter(_CLIENT_CACHE)))
    client = Mem0OSSClient(base_url, api_key)
    _CLIENT_CACHE[key] = client
    return client


def clear_client_cache() -> None:
    """Drop all cached clients (e.g. after a network error)."""
    _CLIENT_CACHE.clear()


def _with_default_filters(
    filters: dict[str, Any] | None, default_user: str, default_agent: str | None
) -> dict[str, Any]:
    """Inject default user_id and agent_id into search filters when absent."""
    result = dict(filters) if filters else {}
    if "user_id" not in result:
        result["user_id"] = default_user
    if default_agent and "agent_id" not in result:
        result["agent_id"] = default_agent
    return result


# --- Server factory --------------------------------------------------------

try:
    _PKG_VERSION = version("mem0-mcp-server")
except PackageNotFoundError:  # pragma: no cover - script runs without metadata
    _PKG_VERSION = "0.0.0"


def create_server() -> FastMCP:
    """Create a FastMCP server usable via stdio or Docker."""

    if not ENV_API_KEY:
        logger.warning(
            "MEM0_API_KEY is not set; health checks will pass, but every tool "
            "invocation will fail until a key is supplied via session config or env vars."
        )

    server = FastMCP(
        f"mem0-mcp-server/{_PKG_VERSION}",
        host=os.getenv("HOST", "0.0.0.0"),
        port=_int_env("PORT", 8081),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(
        description=(
            "Store a new preference, fact, or conversation snippet. "
            "Requires at least one of: user_id, agent_id, or run_id."
        )
    )
    def add_memory(
        text: Annotated[
            str | None,
            Field(
                default=None,
                description="Plain sentence summarizing what to store. Provide this OR `messages`.",
            ),
        ] = None,
        messages: Annotated[
            list[dict[str, str]] | None,
            Field(
                default=None,
                description="Structured conversation history with `role`/`content`. "
                "Use when you have multiple turns.",
            ),
        ] = None,
        user_id: Annotated[
            str | None,
            Field(default=None, description="Override the default user scope for this write."),
        ] = None,
        agent_id: Annotated[
            str | None, Field(default=None, description="Optional agent identifier.")
        ] = None,
        run_id: Annotated[
            str | None, Field(default=None, description="Optional run identifier.")
        ] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            Field(default=None, description="Attach arbitrary metadata JSON to the memory."),
        ] = None,
        expiration_date: Annotated[
            str | None,
            Field(default=None, description="Expiration date in YYYY-MM-DD format."),
        ] = None,
        infer: Annotated[
            bool | None,
            Field(default=None, description="Whether to extract facts from messages. Defaults to True."),
        ] = None,
        memory_type: Annotated[
            str | None,
            Field(default=None, description="Type of memory to store (e.g. 'core')."),
        ] = None,
        prompt: Annotated[
            str | None,
            Field(default=None, description="Custom prompt to use for fact extraction."),
        ] = None,
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Write durable information to Mem0."""

        api_key, default_user, default_agent, base_url = _resolve_settings(ctx)
        if messages:
            try:
                validated_messages = [ToolMessage(**msg) for msg in messages]
            except ValidationError as exc:
                return _error("invalid_messages", str(exc))
        else:
            validated_messages = None
        args = AddMemoryArgs(
            text=text,
            messages=validated_messages,
            user_id=user_id or (default_user if not (agent_id or run_id) else None),
            agent_id=agent_id or default_agent,
            run_id=run_id,
            metadata=metadata,
            expiration_date=expiration_date,
            infer=infer,
            memory_type=memory_type,
            prompt=prompt,
        )
        payload = args.model_dump(exclude_none=True)
        conversation = payload.pop("messages", None)
        if not conversation:
            derived_text = payload.pop("text", None)
            if derived_text:
                conversation = [{"role": "user", "content": derived_text}]
            else:
                return _error(
                    "messages_missing",
                    "Provide either `text` or `messages` so Mem0 knows what to store.",
                )
        else:
            payload.pop("text", None)

        body = {**payload, "messages": conversation}
        return _client(base_url, api_key).add(body)

    @server.tool(
        description=(
            "Run a semantic search over existing memories.\n\n"
            "Use `filters` to narrow results. user_id is automatically added to filters "
            "if not provided. Set `top_k` to limit results, `threshold` for minimum "
            "similarity, and `explain` to include score details."
        )
    )
    def search_memories(
        query: Annotated[str, Field(description="Natural language description of what to find.")],
        filters: Annotated[
            dict[str, Any] | None,
            Field(default=None, description="Additional filter clauses (user_id injected automatically)."),
        ] = None,
        top_k: Annotated[
            int | None, Field(default=None, description="Maximum number of results to return.")
        ] = None,
        threshold: Annotated[
            float | None, Field(default=None, description="Minimum similarity score for results.")
        ] = None,
        explain: Annotated[
            bool | None,
            Field(default=None, description="Include score details for each search result."),
        ] = None,
        show_expired: Annotated[
            bool | None, Field(default=None, description="Include expired memories.")
        ] = None,
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Semantic search against existing memories."""

        api_key, default_user, default_agent, base_url = _resolve_settings(ctx)
        args = SearchMemoriesArgs(
            query=query,
            filters=_with_default_filters(filters, default_user, default_agent),
            top_k=top_k,
            threshold=threshold,
            explain=explain,
            show_expired=show_expired,
        )
        body = args.model_dump(exclude_none=True)
        return _client(base_url, api_key).search(body)

    @server.tool(
        description=(
            "List memories using flat filters (user_id, agent_id, run_id) and optional top_k.\n\n"
            "Unlike the cloud API, the OSS server does not support AND/OR/NOT filter trees; "
            "use the dedicated parameters. user_id defaults to the server's default user."
        )
    )
    def get_memories(
        user_id: Annotated[
            str | None,
            Field(default=None, description="Filter by user ID; defaults to server user."),
        ] = None,
        agent_id: Annotated[
            str | None, Field(default=None, description="Filter by agent ID.")
        ] = None,
        run_id: Annotated[
            str | None, Field(default=None, description="Filter by run ID.")
        ] = None,
        top_k: Annotated[
            int | None, Field(default=None, description="Maximum number of memories (max 1000).")
        ] = None,
        show_expired: Annotated[
            bool | None, Field(default=None, description="Include expired memories.")
        ] = None,
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """List memories via flat filters."""

        api_key, default_user, default_agent, base_url = _resolve_settings(ctx)
        args = GetMemoriesArgs(
            user_id=user_id or default_user,
            agent_id=agent_id or default_agent,
            run_id=run_id,
            top_k=top_k,
            show_expired=show_expired,
        )
        params = args.model_dump(exclude_none=True)
        return _client(base_url, api_key).list_memories(params)

    @server.tool(description="Delete every memory in the given user/agent/run scope but keep the entity.")
    def delete_all_memories(
        user_id: Annotated[
            str | None,
            Field(default=None, description="User scope to delete; defaults to server user."),
        ] = None,
        agent_id: Annotated[
            str | None, Field(default=None, description="Optional agent scope to delete.")
        ] = None,
        run_id: Annotated[
            str | None, Field(default=None, description="Optional run scope to delete.")
        ] = None,
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Bulk-delete every memory in the confirmed scope."""

        api_key, default_user, default_agent, base_url = _resolve_settings(ctx)
        args = DeleteAllArgs(
            user_id=user_id or default_user,
            agent_id=agent_id or default_agent,
            run_id=run_id,
        )
        params = args.model_dump(exclude_none=True)
        return _client(base_url, api_key).delete_all(params)

    @server.tool(description="List which users/agents/runs currently hold memories.")
    def list_entities(ctx: Context[Any, Any] | None = None) -> dict[str, Any] | str:
        """List users/agents/runs with stored memories."""

        api_key, _, _, base_url = _resolve_settings(ctx)
        return _client(base_url, api_key).list_entities()

    @server.tool(description="Fetch a single memory once you know its memory_id.")
    def get_memory(
        memory_id: Annotated[str, Field(description="Exact memory_id to fetch.")],
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Retrieve a single memory once the user has picked an exact ID."""

        api_key, _, _, base_url = _resolve_settings(ctx)
        try:
            return _client(base_url, api_key).get(memory_id)
        except ValueError as exc:
            return _error("invalid_memory_id", str(exc))

    @server.tool(description="Retrieve the edit history of a single memory.")
    def get_memory_history(
        memory_id: Annotated[str, Field(description="Exact memory_id to fetch history for.")],
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Retrieve the edit history of a single memory."""

        api_key, _, _, base_url = _resolve_settings(ctx)
        try:
            return _client(base_url, api_key).history(memory_id)
        except ValueError as exc:
            return _error("invalid_memory_id", str(exc))

    @server.tool(description="Overwrite an existing memory's text and/or metadata.")
    def update_memory(
        memory_id: Annotated[str, Field(description="Exact memory_id to overwrite.")],
        text: Annotated[str | None, Field(default=None, description="Replacement text for the memory.")] = None,
        metadata: Annotated[
            dict[str, Any] | None, Field(default=None, description="Metadata to update.")
        ] = None,
        expiration_date: Annotated[
            str | None, Field(default=None, description="Expiration date in YYYY-MM-DD format.")
        ] = None,
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Overwrite an existing memory after the user confirms the exact memory_id."""

        api_key, _, _, base_url = _resolve_settings(ctx)
        args = UpdateMemoryArgs(text=text, metadata=metadata, expiration_date=expiration_date)
        body = args.model_dump(exclude_none=True)
        if not body:
            return _error(
                "nothing_to_update",
                "Provide at least one of: text, metadata, expiration_date.",
            )
        try:
            return _client(base_url, api_key).update(memory_id, body)
        except ValueError as exc:
            return _error("invalid_memory_id", str(exc))

    @server.tool(description="Delete one memory after the user confirms its memory_id.")
    def delete_memory(
        memory_id: Annotated[str, Field(description="Exact memory_id to delete.")],
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Delete a memory once the user explicitly confirms the memory_id to remove."""

        api_key, _, _, base_url = _resolve_settings(ctx)
        try:
            return _client(base_url, api_key).delete(memory_id)
        except ValueError as exc:
            return _error("invalid_memory_id", str(exc))

    @server.tool(
        description="Remove a user/agent/run record entirely (and cascade-delete its memories)."
    )
    def delete_entities(
        user_id: Annotated[
            str | None, Field(default=None, description="Delete this user and its memories.")
        ] = None,
        agent_id: Annotated[
            str | None, Field(default=None, description="Delete this agent and its memories.")
        ] = None,
        run_id: Annotated[
            str | None, Field(default=None, description="Delete this run and its memories.")
        ] = None,
        ctx: Context[Any, Any] | None = None,
    ) -> dict[str, Any] | str:
        """Delete a user/agent/run (and its memories) once the user confirms the scope."""

        api_key, _, _, base_url = _resolve_settings(ctx)
        args = DeleteEntitiesArgs(user_id=user_id, agent_id=agent_id, run_id=run_id)
        if args.user_id:
            scope: tuple[str, str] = ("user", args.user_id)
        elif args.agent_id:
            scope = ("agent", args.agent_id)
        elif args.run_id:
            scope = ("run", args.run_id)
        else:
            return _error(
                "scope_missing",
                "Provide user_id, agent_id, or run_id before calling delete_entities.",
            )
        try:
            return _client(base_url, api_key).delete_entity(*scope)
        except ValueError as exc:
            return _error("invalid_entity", str(exc))

    @server.prompt()
    def memory_assistant() -> str:
        """Get help with memory operations and best practices."""
        return """You are using the Mem0 MCP server (OSS REST edition) for long-term memory management.

Quick Start:
1. Store memories: Use add_memory to save facts, preferences, or conversations
2. Search memories: Use search_memories for semantic queries
3. List memories: Use get_memories for filtered browsing (flat filters: user_id, agent_id, run_id)
4. Update/Delete: Use update_memory and delete_memory for modifications
5. History: Use get_memory_history to see edits to a single memory

Notes:
- user_id is automatically added to search filters and get_memories defaults
- The OSS server uses flat filters (user_id, agent_id, run_id), not AND/OR/NOT trees
- Graph memory is not available on the OSS server
- Use expiration_date (YYYY-MM-DD) for time-limited memories"""

    return server


def main() -> None:
    """Run the MCP server over stdio."""

    server = create_server()
    logger.info(
        "Starting Mem0 MCP server (OSS, base_url=%s, default user=%s)",
        ENV_BASE_URL,
        ENV_DEFAULT_USER_ID,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
