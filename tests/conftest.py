"""Shared pytest fixtures for the mem0-mcp-server test suite.

Task 2.1 (change: test-suite-foundation) defines the fake mem0 OSS HTTP
server as a Starlette app plus a mutable config object holding the canned
``responses`` dict, the ``received`` request log, and the latency settings.

The uvicorn ``Server`` wiring (port-0 binding, startup polling, teardown) is
added in task 2.2; this file only builds the app + handlers + config and
exposes the ``fake_mem0_server`` fixture yielding ``(app, config)``. No server
is started here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# --- Route keys ------------------------------------------------------------
#
# The ``responses`` dict on the config object is keyed by ``(METHOD, path)``
# tuples using the route *pattern* (with ``{id}`` placeholders), not the
# concrete request path. Each handler closes over its own route key so it can
# look up its canned response without parsing the URL.

ROUTE_POST_MEMORIES = ("POST", "/memories")
ROUTE_POST_SEARCH = ("POST", "/search")
ROUTE_GET_MEMORIES = ("GET", "/memories")
ROUTE_GET_MEMORY = ("GET", "/memories/{id}")
ROUTE_PUT_MEMORY = ("PUT", "/memories/{id}")
ROUTE_DELETE_MEMORIES = ("DELETE", "/memories")
ROUTE_DELETE_MEMORY = ("DELETE", "/memories/{id}")
ROUTE_GET_HISTORY = ("GET", "/memories/{id}/history")
ROUTE_GET_ENTITIES = ("GET", "/entities")
ROUTE_DELETE_ENTITY = ("DELETE", "/entities/{type}/{id}")

# POST/PUT/PATCH/DELETE use write_latency; GET uses read_latency.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _default_responses() -> dict[tuple[str, str], dict[str, Any]]:
    """Canned happy-path bodies matching the mem0 OSS REST surface.

    Shapes mirror what ``Mem0OSSClient`` wrappers receive from the real OSS
    server (``server.py`` returns ``resp.json()`` directly), so integration
    tests assert against realistic payloads. Every entry has ``status`` and
    ``body``; a handler called with no per-test override still returns a valid
    response.
    """
    return {
        ROUTE_POST_MEMORIES: {
            "status": 200,
            "body": {"results": [{"id": "mem-1", "memory": "likes pizza", "event": "ADD"}]},
        },
        ROUTE_POST_SEARCH: {
            "status": 200,
            "body": {"results": [{"id": "mem-1", "memory": "likes pizza", "score": 0.95}]},
        },
        ROUTE_GET_MEMORIES: {
            "status": 200,
            "body": {"results": [{"id": "mem-1", "memory": "likes pizza", "user_id": "u"}]},
        },
        ROUTE_GET_MEMORY: {
            "status": 200,
            "body": {"id": "mem-1", "memory": "likes pizza", "user_id": "u"},
        },
        ROUTE_PUT_MEMORY: {
            "status": 200,
            "body": {"id": "mem-1", "memory": "updated text", "event": "UPDATE"},
        },
        ROUTE_DELETE_MEMORIES: {
            "status": 200,
            "body": {"message": "All memories deleted"},
        },
        ROUTE_DELETE_MEMORY: {
            "status": 200,
            "body": {"id": "mem-1", "event": "DELETE"},
        },
        ROUTE_GET_HISTORY: {
            "status": 200,
            "body": {
                "results": [
                    {
                        "id": "mem-1",
                        "memory": "likes pizza",
                        "event": "ADD",
                        "previous_memory": None,
                    }
                ]
            },
        },
        ROUTE_GET_ENTITIES: {
            "status": 200,
            "body": {"results": []},
        },
        ROUTE_DELETE_ENTITY: {
            "status": 200,
            "body": {"message": "Entity deleted"},
        },
    }


@dataclass
class FakeMem0Config:
    """Mutable config shared between the test thread and the fake server handlers.

    Attributes:
        responses: Keyed by ``(METHOD, path-pattern)``; each value is
            ``{"status": int, "body": Any}``. Tests override entries per-test
            during setup, before any HTTP call, so there is no concurrent
            write/read race on this dict.
        received: Appended to by handlers running on uvicorn/anyio worker
            threads and read by the test thread after calls have joined.
            Appends are guarded by ``lock``.
        write_latency: Seconds slept by POST/PUT/PATCH/DELETE handlers.
        read_latency: Seconds slept by GET handlers.
        lock: Guards appends to ``received`` (cross-thread visibility +
            mutual exclusion for the concurrency tests).
    """

    responses: dict[tuple[str, str], dict[str, Any]] = field(default_factory=_default_responses)
    received: list[dict[str, Any]] = field(default_factory=list)
    write_latency: float = 0.0
    read_latency: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _serve_blocking(
    config: FakeMem0Config,
    route_key: tuple[str, str],
    recorded: dict[str, Any],
    latency: float,
) -> JSONResponse:
    """Blocking portion of a handler: sleep, record, build the JSON response.

    Runs on an anyio threadpool worker thread so ``time.sleep`` blocks only
    that worker, leaving the uvicorn event loop free to handle concurrent
    requests (required by the concurrency tests in tasks 9.1/9.2). The
    ``recorded`` dict is built in the async handler (where the body can be
    awaited); here it is appended to ``config.received`` under lock so the
    cross-thread mutation is guarded.
    """
    if latency > 0:
        time.sleep(latency)
    with config.lock:
        config.received.append(recorded)
    canned = config.responses.get(route_key)
    if canned is None:
        return JSONResponse(
            {"error": "no_canned_response", "route": list(route_key)},
            status_code=500,
        )
    # Graceful fallback: a partial override missing "status" or "body" borrows
    # the missing field from the default entry (or a sane default) instead of
    # raising a raw KeyError. Keeps test-author mistakes diagnostic, not fatal.
    default = _default_responses().get(route_key, {})
    status = canned.get("status", default.get("status", 200))
    canned_body = canned.get("body", default.get("body"))
    if isinstance(canned_body, dict):
        # Dict bodies: merge the request echo inline (echo contract for 7.5/8.4/9.2).
        merged: Any = {**canned_body, "_received": recorded}
    else:
        # Non-dict bodies (list/str/etc.): return as-is without the _received
        # echo — non-dict bodies are not in any task's assertion contract.
        merged = canned_body
    return JSONResponse(merged, status_code=status)


def _make_handler(
    config: FakeMem0Config, route_key: tuple[str, str]
) -> Callable[[Request], Coroutine[Any, Any, JSONResponse]]:
    """Build a Starlette route handler for one route key.

    The handler is ``async def`` so it can read the request body with
    ``await request.json()`` (Starlette's body accessors are async). The
    blocking sleep + recording + response construction run on an anyio
    threadpool worker thread via ``anyio.to_thread.run_sync``, so
    ``time.sleep`` does not block the event loop and concurrent requests are
    served by separate workers.
    """

    async def handler(request: Request) -> JSONResponse:
        try:
            json_body: Any = await request.json()
        except ValueError:
            # No body or unparseable JSON (e.g. GET with an empty body raises
            # json.JSONDecodeError, a ValueError subclass).
            json_body = None
        recorded: dict[str, Any] = {
            "method": request.method,
            "path": request.url.path,
            "query_params": dict(request.query_params),
            "json_body": json_body,
            "headers": {
                "X-API-Key": request.headers.get("X-API-Key"),
                "Content-Type": request.headers.get("Content-Type"),
            },
        }
        method = route_key[0]
        latency = config.write_latency if method in _WRITE_METHODS else config.read_latency
        return await anyio.to_thread.run_sync(
            _serve_blocking, config, route_key, recorded, latency
        )

    return handler


def make_fake_mem0_app(config: FakeMem0Config) -> Starlette:
    """Build the Starlette app with all 10 mem0 OSS REST routes."""
    routes = [
        Route("/memories", _make_handler(config, ROUTE_POST_MEMORIES), methods=["POST"]),
        Route("/search", _make_handler(config, ROUTE_POST_SEARCH), methods=["POST"]),
        Route("/memories", _make_handler(config, ROUTE_GET_MEMORIES), methods=["GET"]),
        Route("/memories/{id}", _make_handler(config, ROUTE_GET_MEMORY), methods=["GET"]),
        Route("/memories/{id}", _make_handler(config, ROUTE_PUT_MEMORY), methods=["PUT"]),
        Route("/memories", _make_handler(config, ROUTE_DELETE_MEMORIES), methods=["DELETE"]),
        Route("/memories/{id}", _make_handler(config, ROUTE_DELETE_MEMORY), methods=["DELETE"]),
        Route(
            "/memories/{id}/history",
            _make_handler(config, ROUTE_GET_HISTORY),
            methods=["GET"],
        ),
        Route("/entities", _make_handler(config, ROUTE_GET_ENTITIES), methods=["GET"]),
        Route(
            "/entities/{type}/{id}",
            _make_handler(config, ROUTE_DELETE_ENTITY),
            methods=["DELETE"],
        ),
    ]
    return Starlette(routes=routes)


@pytest.fixture
def fake_mem0_server() -> Iterator[tuple[Starlette, FakeMem0Config]]:
    """Yield the ``(app, config)`` pair for the fake mem0 OSS HTTP server.

    Task 2.1 scope: only the Starlette app and config object are produced —
    no uvicorn server is started. The app can be driven directly via
    ``starlette.testclient.TestClient`` for shape/import checks.

    TODO(task-2.2): replace the yield below with a real ``uvicorn.Server``
    bound to ``host="127.0.0.1"``, ``port=0`` on a background daemon thread.
    Poll ``server.servers[0].sockets`` until bound (5s deadline -> raise
    ``RuntimeError("fake mem0 server failed to bind within 5s")``), read the
    assigned port via ``getsockname()[1]``, and ``yield (base_url, config)``.
    In teardown set ``server.should_exit = True`` and
    ``thread.join(timeout=5)``. Until then, yield ``(app, config)`` directly.
    """
    config = FakeMem0Config()
    app = make_fake_mem0_app(config)
    yield app, config
