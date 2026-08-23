"""Integration tests for ``Mem0OSSClient`` against the fake mem0 OSS server.

These tests exercise the real ``Mem0OSSClient._call`` HTTP path over a real
socket (the ``fake_mem0_server`` fixture from ``tests/conftest.py`` serves a
Starlette app on an ephemeral port via ``uvicorn.Server``). No ``requests``
transport adapter is monkeypatched — the client's ``requests.Session`` makes
actual TCP calls to the fake server, so the request-construction, header,
timeout, and error-mapping code paths in ``server.py`` are exercised end to
end.

Coverage by task (see ``openspec/changes/test-suite-foundation/tasks.md``):

- 7.1: ``_call`` happy path (``GET /entities`` returns the canned JSON).
- 7.2: HTTP error mapping (4xx/5xx -> ``_error("http_<status>", ...,
  status=<status>)``).
- 7.3: ``requests.RequestException`` handling (closed port ->
  ``_error("http_request_failed", ...)`` with no ``status`` key, and
  ``clear_client_cache`` is invoked).
- 7.4: timeout selection (write sentinel for POST/PUT/PATCH/DELETE-via-
  ``delete_all``; read sentinel for GET).
- 7.5: ``functools.partial`` kwarg survival — ``method``/``url``/``params``/
  ``json`` all reach the wire (read from the fake server's ``received`` echo).
- 7.6: happy-path coverage for all 10 wrapper methods.
"""

from __future__ import annotations

import socket as _socket
from collections.abc import Iterator
from typing import Any

import pytest

from mem0_mcp_server import server
from mem0_mcp_server.server import (
    Mem0OSSClient,
    _client,
    clear_client_cache,
)
from tests.conftest import (
    ROUTE_GET_ENTITIES,
    ROUTE_POST_MEMORIES,
    FakeMem0Config,
)

_API_KEY = "test-integration-api-key-aaaaaaaa"


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterator[None]:
    """Clear ``_CLIENT_CACHE`` before and after every test in this module.

    ``_CLIENT_CACHE`` is module-level mutable state shared across the whole
    process. A test that constructs a client (directly or via ``_client``)
    would otherwise leak that entry into later tests, so a later test's
    "fresh client bound to a new port" could resolve to a stale cached client
    pointing at a torn-down fake server. Clearing before *and* after each test
    keeps each test isolated and deterministic regardless of pass/fail.
    """
    clear_client_cache()
    yield
    clear_client_cache()


# ---------------------------------------------------------------------------
# Task 7.1 — ``_call`` happy path
# ---------------------------------------------------------------------------


def test_call_get_entities_returns_canned_body(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``Mem0OSSClient._call("GET", "/entities")`` returns the canned JSON body.

    Constructs a ``Mem0OSSClient`` against the fake server's base URL and
    issues a ``GET /entities`` through ``_call`` (the same code path the
    ``list_entities`` wrapper uses). The fake server's default canned response
    for ``GET /entities`` is ``{"results": []}`` (see ``_default_responses`` in
    ``tests/conftest.py``); the client returns ``resp.json()`` directly on a
    2xx (``server.py:271``), so the assertion pins both the happy-path return
    shape and the fact that no error dict is produced for a 200.
    """
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client._call("GET", "/entities")

    # The fake server merges a ``_received`` echo into dict bodies (task 2.1
    # echo contract for 7.5/8.4/9.2); the real OSS server would return only
    # the canned fields. Assert the canned content is present and no error
    # dict was produced (an error would have an ``error`` key, not
    # ``results``).
    assert "error" not in result
    assert result["results"] == []


# ---------------------------------------------------------------------------
# Task 7.2 — HTTP error mapping
# ---------------------------------------------------------------------------
#
# ``_call`` (``server.py:261-269``) maps any ``status_code >= 400`` to
# ``_error(f"http_{status}", _redact(resp.text, 1000), status=status)``, which
# produces ``{"error": "http_<status>", "detail": <redacted text>,
# "status": <status>}``. The tests below override the fake server's
# ``responses`` dict per-test to return a 404 (GET) and a 500 (POST), then
# assert the exact error-code string, the presence of the ``status`` field,
# and that ``detail`` is a non-empty string (the redacted response body).


def test_call_maps_get_404_to_http_404_error(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """A GET returning 404 maps to ``_error("http_404", ..., status=404)``.

    Overrides ``GET /entities`` to return status 404 with a descriptive body,
    then asserts the client returns the standardized error dict with the
    correct code, the numeric ``status`` field, and a non-empty ``detail``
    carrying (redacted) response text.
    """
    base_url, config = fake_mem0_server
    config.responses[ROUTE_GET_ENTITIES] = {
        "status": 404,
        "body": {"error": "not_found", "message": "entity does not exist"},
    }
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client._call("GET", "/entities")

    assert result["error"] == "http_404"
    assert result["status"] == 404
    assert isinstance(result["detail"], str)
    assert result["detail"]  # non-empty


def test_call_maps_post_500_to_http_500_error(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """A POST returning 500 maps to ``_error("http_500", ..., status=500)``.

    Overrides ``POST /memories`` to return status 500 with a server-error
    body, then asserts the client returns the standardized error dict with
    ``error == "http_500"``, ``status == 500``, and a non-empty ``detail``.
    """
    base_url, config = fake_mem0_server
    config.responses[ROUTE_POST_MEMORIES] = {
        "status": 500,
        "body": {"error": "internal_error", "message": "embedding service down"},
    }
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client._call("POST", "/memories", json_body={"messages": []})

    assert result["error"] == "http_500"
    assert result["status"] == 500
    assert isinstance(result["detail"], str)
    assert result["detail"]  # non-empty


def test_call_error_dict_has_no_extra_keys(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """The error dict from a 4xx/5xx contains exactly ``error``, ``detail``,
    ``status`` — no extra keys leak in.

    ``_error`` (``server.py:114-119``) builds ``{"error": ..., "detail": ...}``
    and only adds ``"status"`` when it is not ``None``. The HTTP-error path
    always passes ``status=resp.status_code`` (non-None), so the result must
    have exactly three keys. A regression that added, e.g., a ``message`` key
    would fail here.
    """
    base_url, config = fake_mem0_server
    config.responses[ROUTE_GET_ENTITIES] = {"status": 403, "body": {"error": "forbidden"}}
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client._call("GET", "/entities")

    assert set(result.keys()) == {"error", "detail", "status"}
    assert result["error"] == "http_403"
    assert result["status"] == 403


# ---------------------------------------------------------------------------
# Task 7.3 — ``requests.RequestException`` handling
# ---------------------------------------------------------------------------
#
# ``_call`` (``server.py:256-259``) catches ``requests.RequestException``,
# calls ``clear_client_cache()``, and returns ``_error("http_request_failed",
# str(exc))`` — note: no ``status`` argument, so the error dict has no
# ``status`` key (unlike the HTTP 4xx/5xx path). The test points a
# ``Mem0OSSClient`` at a closed port (bind a socket to grab a free port, then
# close it) so ``requests`` raises ``ConnectionError`` (a
# ``RequestException`` subclass). It populates ``_CLIENT_CACHE`` via
# ``_client(...)`` first, then asserts the cache is empty after the failed
# call — proving ``clear_client_cache`` was invoked, not just that the error
# dict was returned.


def _grab_free_port() -> int:
    """Bind a socket to port 0, read the assigned port, close the socket.

    After this returns, nothing is listening on the port (the socket was
    closed), so a ``requests`` connection attempt to it raises
    ``ConnectionError`` deterministically — no fake-server support needed.
    The OS may reassign the port before we connect, but on the test timescale
    this is reliable enough (and the task spec explicitly calls this approach
    deterministic).
    """
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_call_returns_http_request_failed_on_closed_port() -> None:
    """A ``ConnectionError`` (closed port) maps to
    ``_error("http_request_failed", ...)`` with no ``status`` key, and
    ``clear_client_cache`` is invoked.

    The autouse ``_reset_client_cache`` fixture handles cache isolation; no
    fake server is needed (the call goes to a closed port). We first populate
    ``_CLIENT_CACHE`` via ``_client(...)`` so the post-failure emptiness check
    is meaningful (an already-empty cache would trivially satisfy it).
    """
    closed_port = _grab_free_port()
    client = Mem0OSSClient(f"http://127.0.0.1:{closed_port}", _API_KEY)

    # Populate the cache so the post-failure emptiness assertion is meaningful.
    _client("http://127.0.0.1:1", "populate-cache-key-aaaaaaaaaaaaaaaa")
    assert len(server._CLIENT_CACHE) == 1

    result = client._call("GET", "/entities")

    assert result["error"] == "http_request_failed"
    assert "status" not in result  # RequestException path omits status
    assert isinstance(result["detail"], str)
    assert result["detail"]  # non-empty (the exception message)

    # ``clear_client_cache`` was invoked by the exception handler.
    assert len(server._CLIENT_CACHE) == 0


# ---------------------------------------------------------------------------
# Task 7.4 — timeout selection
# ---------------------------------------------------------------------------
#
# ``_call`` (``server.py:246-247``) selects the timeout when the caller does
# not pass one explicitly: ``_WRITE_TIMEOUT`` for POST/PUT/PATCH,
# ``_READ_TIMEOUT`` otherwise (GET, DELETE). ``delete_all`` (``server.py:298``)
# passes ``timeout=_WRITE_TIMEOUT`` explicitly even though its method is
# DELETE — so the write timeout is used regardless of the method-based branch.
#
# The test monkeypatches ``_WRITE_TIMEOUT`` / ``_READ_TIMEOUT`` to distinct
# sentinel values and wraps ``requests.Session.request`` with a spy that
# records the ``timeout`` kwarg. No wall-clock sleeps are used — the spy is
# deterministic and fast. The fake server responds immediately (latency 0).

_WRITE_SENTINEL = 999
_READ_SENTINEL = 888


def test_call_selects_write_timeout_for_post(
    fake_mem0_server: tuple[str, FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST passes the write sentinel timeout to ``requests``."""
    monkeypatch.setattr(server, "_WRITE_TIMEOUT", _WRITE_SENTINEL)
    monkeypatch.setattr(server, "_READ_TIMEOUT", _READ_SENTINEL)
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    seen: list[Any] = []
    _wrap_request_with_timeout_spy(client, seen, monkeypatch)

    client._call("POST", "/memories", json_body={"messages": []})

    assert seen == [_WRITE_SENTINEL]


def test_call_selects_write_timeout_for_put(
    fake_mem0_server: tuple[str, FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT passes the write sentinel timeout to ``requests``."""
    monkeypatch.setattr(server, "_WRITE_TIMEOUT", _WRITE_SENTINEL)
    monkeypatch.setattr(server, "_READ_TIMEOUT", _READ_SENTINEL)
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    seen: list[Any] = []
    _wrap_request_with_timeout_spy(client, seen, monkeypatch)

    client._call("PUT", "/memories/mem-1", json_body={"text": "updated"})

    assert seen == [_WRITE_SENTINEL]


def test_call_selects_read_timeout_for_get(
    fake_mem0_server: tuple[str, FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET passes the read sentinel timeout to ``requests``."""
    monkeypatch.setattr(server, "_WRITE_TIMEOUT", _WRITE_SENTINEL)
    monkeypatch.setattr(server, "_READ_TIMEOUT", _READ_SENTINEL)
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    seen: list[Any] = []
    _wrap_request_with_timeout_spy(client, seen, monkeypatch)

    client._call("GET", "/entities")

    assert seen == [_READ_SENTINEL]


def test_call_selects_read_timeout_for_plain_delete(
    fake_mem0_server: tuple[str, FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain ``_call("DELETE", ...)`` (no explicit timeout) falls into the
    read branch — DELETE is not in the ``(POST, PUT, PATCH)`` set — so the
    read sentinel is passed.

    This is the complementary case to ``delete_all`` (which passes the write
    timeout explicitly); together they prove the method-based selection and
    the explicit-override path are independent.
    """
    monkeypatch.setattr(server, "_WRITE_TIMEOUT", _WRITE_SENTINEL)
    monkeypatch.setattr(server, "_READ_TIMEOUT", _READ_SENTINEL)
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    seen: list[Any] = []
    _wrap_request_with_timeout_spy(client, seen, monkeypatch)

    client._call("DELETE", "/memories/mem-1")

    assert seen == [_READ_SENTINEL]


def test_delete_all_passes_write_timeout_explicitly(
    fake_mem0_server: tuple[str, FakeMem0Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delete_all`` passes ``timeout=_WRITE_TIMEOUT`` explicitly, so the
    write sentinel reaches ``requests`` even though the method is DELETE
    (which would otherwise select the read timeout).

    ``server.py:298``: ``self._call("DELETE", "/memories", params=params,
    timeout=_WRITE_TIMEOUT)``. Because ``_call`` uses the explicit timeout
    when it is not ``None`` (``server.py:246``), the method-based branch is
    bypassed. This test guards against a refactor that drops the explicit
    ``timeout=`` from ``delete_all`` — without it, a bulk delete would use the
    read timeout (60s default), which is too short for the LLM-backed delete
    path on local models.
    """
    monkeypatch.setattr(server, "_WRITE_TIMEOUT", _WRITE_SENTINEL)
    monkeypatch.setattr(server, "_READ_TIMEOUT", _READ_SENTINEL)
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    seen: list[Any] = []
    _wrap_request_with_timeout_spy(client, seen, monkeypatch)

    client.delete_all({"user_id": "u"})

    assert seen == [_WRITE_SENTINEL]


def _wrap_request_with_timeout_spy(
    client: Mem0OSSClient,
    seen: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrap ``client._session.request`` to record the ``timeout`` kwarg.

    The spy delegates to the original ``request`` so the real HTTP call still
    happens (the fake server responds immediately at latency 0). The recorded
    timeout is appended to ``seen`` in call order.

    ``monkeypatch.setattr`` is used so the original ``request`` is restored
    automatically at test teardown, even if the test fails mid-execution.
    """
    original = client._session.request

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("timeout"))
        return original(*args, **kwargs)

    monkeypatch.setattr(client._session, "request", spy)


# ---------------------------------------------------------------------------
# Task 7.5 — ``functools.partial`` kwarg survival
# ---------------------------------------------------------------------------
#
# Guard against a misbound ``functools.partial`` silently dropping an
# argument. The test asserts that ``method``, ``url``, ``params``, and
# ``json`` all reach the wire by reading the fake server's ``received`` echo
# (task 2.1): each handler records ``(method, path, query_params, json_body,
# headers)`` into ``config.received``. ``timeout`` is not observable on the
# wire, so it is covered separately by the spy in task 7.4.
#
# This test is green on sync code (direct ``_call``) and must remain green
# after the async refactor (partial binding) — it is the characterization
# proof that no argument is dropped.


def test_call_kwargs_survive_to_the_wire(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``method``, ``url``, ``params``, and ``json`` all reach the wire.

    Issues two calls through ``_call`` — a GET with query params and a POST
    with a JSON body — then reads the fake server's ``received`` log and
    asserts the recorded method, path, query params, and JSON body match
    exactly what ``_call`` was given. If a refactor (e.g. ``functools.partial``
    binding) silently dropped ``params`` or ``json``, the recorded request
    would not match and this test would fail.
    """
    base_url, config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    client._call("GET", "/memories", params={"user_id": "u1", "top_k": "5"})
    client._call(
        "POST",
        "/search",
        json_body={"query": "pizza", "filters": {"user_id": "u1"}},
    )

    assert len(config.received) == 2

    get_entry, post_entry = config.received

    # GET: method, path, query params reach the wire; json_body is None.
    assert get_entry["method"] == "GET"
    assert get_entry["path"] == "/memories"
    assert get_entry["query_params"] == {"user_id": "u1", "top_k": "5"}
    assert get_entry["json_body"] is None

    # POST: method, path, json body reach the wire; query params are empty.
    assert post_entry["method"] == "POST"
    assert post_entry["path"] == "/search"
    assert post_entry["query_params"] == {}
    assert post_entry["json_body"] == {"query": "pizza", "filters": {"user_id": "u1"}}


# ---------------------------------------------------------------------------
# Task 7.6 — happy-path tests for all 10 wrapper methods
# ---------------------------------------------------------------------------
#
# Each wrapper method on ``Mem0OSSClient`` (``server.py:276-311``) delegates
# to ``_call`` with the correct method, path, and payload. The tests below
# call each wrapper against the fake server and verify the canned response is
# returned. The fake server merges a ``_received`` echo into dict bodies, so
# assertions check the canned content (not the full dict) and the absence of
# an ``error`` key.


def test_wrapper_add_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``add(body)`` → POST /memories → canned write result."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.add({"messages": [{"role": "user", "content": "likes pizza"}], "user_id": "u"})

    assert "error" not in result
    assert result["results"] == [{"id": "mem-1", "memory": "likes pizza", "event": "ADD"}]


def test_wrapper_search_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``search(body)`` → POST /search → canned search result."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.search({"query": "pizza", "filters": {"user_id": "u"}})

    assert "error" not in result
    assert result["results"] == [{"id": "mem-1", "memory": "likes pizza", "score": 0.95}]


def test_wrapper_list_memories_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``list_memories(params)`` → GET /memories → canned list."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.list_memories({"user_id": "u"})

    assert "error" not in result
    assert result["results"] == [{"id": "mem-1", "memory": "likes pizza", "user_id": "u"}]


def test_wrapper_get_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``get(memory_id)`` → GET /memories/{id} → canned single memory."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.get("mem-1")

    assert "error" not in result
    assert result["id"] == "mem-1"
    assert result["memory"] == "likes pizza"
    assert result["user_id"] == "u"


def test_wrapper_update_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``update(memory_id, body)`` → PUT /memories/{id} → canned update result."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.update("mem-1", {"text": "updated"})

    assert "error" not in result
    assert result["id"] == "mem-1"
    assert result["memory"] == "updated text"
    assert result["event"] == "UPDATE"


def test_wrapper_delete_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``delete(memory_id)`` → DELETE /memories/{id} → canned delete result."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.delete("mem-1")

    assert "error" not in result
    assert result["id"] == "mem-1"
    assert result["event"] == "DELETE"


def test_wrapper_delete_all_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``delete_all(params)`` → DELETE /memories → canned bulk-delete result."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.delete_all({"user_id": "u"})

    assert "error" not in result
    assert result["message"] == "All memories deleted"


def test_wrapper_history_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``history(memory_id)`` → GET /memories/{id}/history → canned history."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.history("mem-1")

    assert "error" not in result
    assert result["results"] == [
        {"id": "mem-1", "memory": "likes pizza", "event": "ADD", "previous_memory": None}
    ]


def test_wrapper_list_entities_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``list_entities()`` → GET /entities → canned entity list."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.list_entities()

    assert "error" not in result
    assert result["results"] == []


def test_wrapper_delete_entity_returns_canned_response(
    fake_mem0_server: tuple[str, FakeMem0Config],
) -> None:
    """``delete_entity(type, id)`` → DELETE /entities/{type}/{id} → canned result."""
    base_url, _config = fake_mem0_server
    client = Mem0OSSClient(base_url, _API_KEY)

    result = client.delete_entity("user", "u")

    assert "error" not in result
    assert result["message"] == "Entity deleted"
