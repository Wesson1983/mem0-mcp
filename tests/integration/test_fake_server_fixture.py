"""Tests for the ``fake_mem0_server`` fixture (task 2.2).

Verifies the two behaviors the task spec requires:

1. The happy path: a real ``uvicorn.Server`` is bound to an ephemeral port and
   a ``requests.get`` to ``GET /entities`` receives the canned response body
   over a real socket (not via ``TestClient``).
2. The timeout path: the bind-poll helper raises ``RuntimeError`` (instead of
   hanging) when the server never binds, completing in well under the 5s
   deadline thanks to a short ``timeout`` argument.
"""

from __future__ import annotations

import socket as _socket
import threading
import time
from typing import Any

import pytest
import requests
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.conftest import (
    FakeMem0Config,
    _wait_for_server_bound,
)


def test_fake_mem0_server_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """The fixture serves the canned ``GET /entities`` body over a real socket."""
    base_url, config = fake_mem0_server

    # Mutate the canned response to prove the config is mutable and live.
    config.responses[("GET", "/entities")] = {
        "status": 200,
        "body": {"results": [{"id": "ent-1", "name": "alice"}]},
    }

    resp = requests.get(f"{base_url}/entities", timeout=5)
    assert resp.status_code == 200
    assert resp.json()["results"] == [{"id": "ent-1", "name": "alice"}]


# Suppress the expected ``SystemExit`` from uvicorn's startup-failure exit
# (forwarded via ``threading.excepthook`` to
# ``PytestUnhandledThreadExceptionWarning``); without this the expected
# startup failure would emit a noisy warning.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_wait_for_server_bound_raises_on_unstartable_app() -> None:
    """The bind-poll helper fails fast with ``RuntimeError`` when the server cannot bind.

    Unit-level check: occupies a free port with a holding socket, then points a
    ``uvicorn.Server`` at the same ``host:port`` so ``loop.create_server``
    raises ``OSError`` (address already in use) during startup and
    ``server.servers`` is never populated. ``_wait_for_server_bound`` is called
    with a short ``timeout`` so the test completes in well under 5s and asserts
    ``RuntimeError`` rather than hanging.

    The fixture-level failure mode is covered by
    ``test_fake_mem0_server_raises_on_unstartable_app`` below, which exercises
    the spec's "the fixture raises" wording directly.
    """
    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    occupied_port = holder.getsockname()[1]

    # The handler is never reached: the server never binds (port occupied), so
    # uvicorn's ``startup`` raises ``OSError`` and exits before serving. A valid
    # no-op handler is used for correctness in case the bind ever succeeded.
    app = Starlette(routes=[Route("/entities", lambda request: JSONResponse({}))])
    uv_config = uvicorn.Config(
        app, host="127.0.0.1", port=occupied_port, log_level="warning"
    )
    server = uvicorn.Server(uv_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="failed to bind"):
            _wait_for_server_bound(server, timeout=0.5)
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)
        holder.close()

    elapsed = time.monotonic() - start
    # Must fail fast (well under the 5s suite deadline), not hang.
    assert elapsed < 2.0


# Suppress the expected ``SystemExit`` from uvicorn's startup-failure exit
# (forwarded via ``threading.excepthook`` to
# ``PytestUnhandledThreadExceptionWarning``); without this the expected
# startup failure would emit a noisy warning.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_fake_mem0_server_raises_on_unstartable_app(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """The ``fake_mem0_server`` fixture raises ``RuntimeError`` (not hangs) on an unstartable app.

    Fixture-level check (spec tasks.md 2.2: "the fixture raises"). We
    monkeypatch ``make_fake_mem0_app`` to return an app that signals
    ``lifespan.startup.failed``, so uvicorn's ``startup`` exits before
    ``loop.create_server``, ``server.servers`` stays ``None``, and the
    fixture's bind-poll times out. ``_BIND_DEADLINE`` is shrunk to ``0.5`` so
    the test completes in well under 5s. Requesting the fixture via
    ``request.getfixturevalue`` inside ``pytest.raises`` is the standard way
    to assert a fixture raises during setup.
    """

    class _UnstartableApp:
        """ASGI app that signals ``lifespan.startup.failed``.

        uvicorn calls the app with ``scope["type"] == "lifespan"`` during
        ``Server.startup``. Sending ``lifespan.startup.failed`` sets
        ``LifespanOn.startup_failed`` (checked regardless of the ``lifespan``
        config mode), so ``Server.startup`` calls ``sys.exit(STARTUP_FAILURE)``
        before ``loop.create_server`` and ``server.servers`` is never populated.
        A plain ``raise`` in ``__call__`` would NOT work: the fixture's
        ``uvicorn.Config`` uses the default ``lifespan="auto"``, which treats a
        raised exception as "lifespan unsupported" and lets startup continue.
        """

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "lifespan":
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send(
                        {"type": "lifespan.startup.failed", "message": "unstartable"}
                    )
                    return

    def _unstartable_app(_config: FakeMem0Config) -> Any:
        return _UnstartableApp()

    monkeypatch.setattr("tests.conftest.make_fake_mem0_app", _unstartable_app)
    monkeypatch.setattr("tests.conftest._BIND_DEADLINE", 0.5)

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="failed to bind"):
        request.getfixturevalue("fake_mem0_server")
    elapsed = time.monotonic() - start
    # Must fail fast (well under the 5s suite deadline), not hang.
    assert elapsed < 2.0
