"""Unit tests for the ``_client`` cache and ``clear_client_cache`` (tasks 5.1/5.2).

``_client(base_url, api_key)`` (``server.py:318-327``) memoizes
``Mem0OSSClient`` instances in the module-level ``_CLIENT_CACHE`` dict, keyed by
``(base_url, sha256(api_key)[:16])``. The cache is bounded by
``_CLIENT_CACHE_MAX`` (default 32); when full, the oldest entry (first by dict
insertion order) is evicted before inserting the new one.

Task 5.1 covers:
- **Identity / caching**: the same ``(base_url, api_key)`` returns the same
  ``Mem0OSSClient`` instance (``is``); a different ``api_key`` or a different
  ``base_url`` returns a distinct instance.
- **Eviction**: with ``_CLIENT_CACHE_MAX`` monkeypatched to a small value (2),
  inserting a third client evicts the oldest key and
  ``len(_CLIENT_CACHE)`` never exceeds the max. This avoids creating 32 real
  clients (each builds a ``requests.Session``) to exercise the bound.

Task 5.2 covers ``clear_client_cache``: after clearing, subsequent ``_client``
calls create new instances instead of returning cached ones.

Cache-state isolation:
  ``_CLIENT_CACHE`` is module-level mutable state shared across the whole
  process. Without isolation, a test that populates the cache would leak
  entries into later tests, making identity assertions non-deterministic
  (a later test's "new instance" could be a prior test's cached instance).
  The ``_reset_client_cache`` fixture (autouse) calls
  ``clear_client_cache()`` before and after every test in this module so each
  test starts and ends with an empty cache.

Why ``monkeypatch.setattr(server, "_CLIENT_CACHE_MAX", 2)`` and not a real
32-client run:
  The eviction branch (``server.py:323-324``) is ``if len(_CLIENT_CACHE) >=
  _CLIENT_CACHE_MAX: _CLIENT_CACHE.pop(next(iter(_CLIENT_CACHE)))``. Creating
  32 real ``Mem0OSSClient`` instances (each constructing a
  ``requests.Session``) to reach the default bound is slow and exercises
  nothing the branch doesn't already exercise at ``max=2``. Lowering the max
  to 2 means the third insertion triggers eviction deterministically and the
  "oldest key dropped" + "len never exceeds max" invariants are checkable with
  three clients, not 33.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mem0_mcp_server import server
from mem0_mcp_server.server import (
    _CLIENT_CACHE,
    Mem0OSSClient,
    _client,
    clear_client_cache,
)

# Distinct, deterministic base URLs / API keys used across the tests. Local
# hosts are used so the values would be accepted by ``_validate_base_url`` if
# they ever flowed through it (they do not here — ``_client`` does not
# validate), keeping the fixtures realistic without coupling to validation.
_BASE_A = "http://localhost:8001"
_BASE_B = "http://localhost:8002"
_KEY_A = "test-api-key-aaaaaaaaaaaaaaaa"
_KEY_B = "test-api-key-bbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Iterator[None]:
    """Clear ``_CLIENT_CACHE`` before and after each test.

    ``_CLIENT_CACHE`` is module-level mutable state. Without this fixture a
    test that populates the cache leaks entries into later tests, making the
    identity ("same instance") and eviction ("oldest key dropped")
    assertions non-deterministic. Clearing before *and* after means each test
    starts empty and leaves the cache empty regardless of pass/fail.
    """
    clear_client_cache()
    yield
    clear_client_cache()


# ---------------------------------------------------------------------------
# Task 5.1 — ``_client`` caching / identity
# ---------------------------------------------------------------------------


def test_client_returns_same_instance_for_same_base_url_and_api_key() -> None:
    """The same ``(base_url, api_key)`` returns the same ``Mem0OSSClient``
    instance (identity via ``is``), not an equal-but-distinct copy.

    ``_client`` builds the cache key as ``(base_url, sha256(api_key)[:16])``
    (``server.py:319``). On a cache hit it returns the stored instance
    directly (``server.py:321-322``), so the second call must be the very same
    object as the first. ``is`` (not ``==``) is the assertion: two distinct
    ``Mem0OSSClient`` instances with equal fields would satisfy ``==`` only if
    ``__eq__`` were defined (it is not — it inherits identity from
    ``object``), but the intent here is to pin the memoization contract, not
    object equality.
    """
    first = _client(_BASE_A, _KEY_A)
    second = _client(_BASE_A, _KEY_A)

    assert first is second
    assert isinstance(first, Mem0OSSClient)


def test_client_returns_distinct_instance_for_different_api_key() -> None:
    """A different ``api_key`` (same ``base_url``) produces a distinct
    ``Mem0OSSClient`` instance.

    The cache key hashes the API key (``server.py:319``), so a different key
    yields a different cache entry and a freshly constructed client. The two
    instances must not be the same object (``is not``), proving the key
    derivation includes the API key and that a miss constructs a new client
    rather than reusing the existing one.
    """
    first = _client(_BASE_A, _KEY_A)
    second = _client(_BASE_A, _KEY_B)

    assert first is not second
    assert isinstance(first, Mem0OSSClient)
    assert isinstance(second, Mem0OSSClient)


def test_client_returns_distinct_instance_for_different_base_url() -> None:
    """A different ``base_url`` (same ``api_key``) produces a distinct
    ``Mem0OSSClient`` instance.

    The cache key is ``(base_url, sha256(api_key)[:16])`` (``server.py:319``):
    ``base_url`` is used verbatim (not hashed), so a different base URL yields
    a different key and a cache miss. The two instances must not be the same
    object, proving the key includes the base URL.
    """
    first = _client(_BASE_A, _KEY_A)
    second = _client(_BASE_B, _KEY_A)

    assert first is not second
    assert isinstance(first, Mem0OSSClient)
    assert isinstance(second, Mem0OSSClient)


def test_client_populates_cache_after_first_call() -> None:
    """After the first ``_client(base_url, api_key)`` call, the cache holds
    exactly one entry keyed by the derived ``(base_url, hash)`` tuple.

    This pins the side effect of ``_client`` (it stores the new instance in
    ``_CLIENT_CACHE``, ``server.py:326``) so a regression that constructed a
    client without caching it would be caught by the later identity tests
    failing (and by this test directly).
    """
    client = _client(_BASE_A, _KEY_A)

    assert len(_CLIENT_CACHE) == 1
    # The single stored value is the instance we just received.
    stored = next(iter(_CLIENT_CACHE.values()))
    assert stored is client
    # The key's base_url component matches the argument verbatim.
    key = next(iter(_CLIENT_CACHE.keys()))
    assert key[0] == _BASE_A


# ---------------------------------------------------------------------------
# Task 5.1 — eviction (``_CLIENT_CACHE_MAX`` bound)
# ---------------------------------------------------------------------------


def test_client_evicts_oldest_entry_when_cache_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_CLIENT_CACHE`` reaches ``_CLIENT_CACHE_MAX``, inserting one more
    client evicts the oldest (first-inserted) key.

    ``_client`` evicts via ``_CLIENT_CACHE.pop(next(iter(_CLIENT_CACHE)))``
    (``server.py:324``), which removes the first key by dict insertion order
    (FIFO). With ``_CLIENT_CACHE_MAX`` monkeypatched to 2, the first two calls
    fill the cache; the third call must evict the first key (``_BASE_A``) and
    insert the third. After the third call:

    - ``len(_CLIENT_CACHE) == 2`` (never exceeds the max).
    - The evicted key (``_BASE_A``) is absent.
    - The two surviving keys are the second (``_BASE_B``) and third
      (``_BASE_C``) base URLs, in insertion order.

    Monkeypatching the max to 2 (instead of creating 33 clients to hit the
    default 32) keeps the test fast and deterministic while exercising the
    exact eviction branch.
    """
    monkeypatch.setattr(server, "_CLIENT_CACHE_MAX", 2)

    base_c = "http://localhost:8003"

    c1 = _client(_BASE_A, _KEY_A)
    c2 = _client(_BASE_B, _KEY_A)
    # Cache is now full (2 entries): [_BASE_A, _BASE_B].
    assert len(_CLIENT_CACHE) == 2

    c3 = _client(base_c, _KEY_A)
    # Third insertion triggers eviction of the oldest key (_BASE_A).

    # The max is never exceeded.
    assert len(_CLIENT_CACHE) == 2
    assert len(_CLIENT_CACHE) <= server._CLIENT_CACHE_MAX

    # The oldest key (first inserted, _BASE_A) was dropped.
    keys = [k for k, _ in _CLIENT_CACHE.items()]
    assert all(k[0] != _BASE_A for k in keys)

    # The two surviving entries are the second and third, in insertion order.
    assert [k[0] for k in keys] == [_BASE_B, base_c]

    # The evicted client is not returned for its old key on a subsequent call:
    # a new call with the evicted (base_url, api_key) constructs a fresh
    # instance (not ``c1``).
    c1_again = _client(_BASE_A, _KEY_A)
    assert c1_again is not c1
    # And that re-insertion evicts the now-oldest key (_BASE_B).
    assert all(k[0] != _BASE_B for k in _CLIENT_CACHE)
    assert len(_CLIENT_CACHE) == 2

    # The three clients constructed are all distinct objects.
    assert c1 is not c2
    assert c2 is not c3
    assert c1 is not c3


def test_client_cache_never_exceeds_max_across_many_inserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across many insertions with a small max, ``len(_CLIENT_CACHE)`` never
    exceeds ``_CLIENT_CACHE_MAX`` at any point.

    This is the invariant form of the eviction test: rather than asserting
    only the post-state of a single eviction, it inserts 10 distinct clients
    with ``max=3`` and checks the bound holds after every insertion. A
    regression that evicted too late (e.g. ``>`` instead of ``>=``) or not at
    all would let the cache grow past the max and fail here.
    """
    monkeypatch.setattr(server, "_CLIENT_CACHE_MAX", 3)

    for i in range(10):
        _client(f"http://localhost:{9000 + i}", _KEY_A)
        assert len(_CLIENT_CACHE) <= server._CLIENT_CACHE_MAX, (
            f"cache exceeded max after insertion {i}: "
            f"len={len(_CLIENT_CACHE)} max={server._CLIENT_CACHE_MAX}"
        )

    # Final state: the last 3 inserted clients survive (FIFO eviction).
    assert len(_CLIENT_CACHE) == 3
    surviving_ports = [int(k[0].rsplit(":", 1)[1]) for k in _CLIENT_CACHE]
    assert surviving_ports == [9007, 9008, 9009]


def test_client_does_not_evict_when_below_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cache is below ``_CLIENT_CACHE_MAX``, inserting a new client
    does not evict any existing entry.

    The eviction branch is ``if len(_CLIENT_CACHE) >= _CLIENT_CACHE_MAX``
    (``server.py:323``), checked *before* each insert. With ``max=3`` and only
    2 entries present, a third insertion brings the cache to exactly ``max``
    — the check (``2 >= 3``) is false, so no eviction occurs and all three
    entries survive. This pins the "no eviction below the bound" behavior.

    Note: this test fills the cache to *exactly* ``max``, the point where
    ``>=`` and ``>`` agree (both skip eviction at ``len < max``). It therefore
    does not by itself distinguish ``>=`` from ``>``; that distinction is
    exercised by ``test_client_cache_never_exceeds_max_across_many_inserts``,
    which inserts *past* ``max`` and asserts ``len <= max`` after every insert
    — a ``>`` regression would let the cache grow to ``max + 1`` there. This
    test instead guards the complementary property: no spurious eviction while
    the cache is still below capacity (an unconditional ``pop`` or an
    off-by-one ``>= max - 1`` would evict here and fail).
    """
    monkeypatch.setattr(server, "_CLIENT_CACHE_MAX", 3)

    c1 = _client(_BASE_A, _KEY_A)
    c2 = _client(_BASE_B, _KEY_A)
    assert len(_CLIENT_CACHE) == 2

    c3 = _client("http://localhost:8003", _KEY_A)

    # All three survive — no eviction below the max.
    assert len(_CLIENT_CACHE) == 3
    assert all(c in _CLIENT_CACHE.values() for c in (c1, c2, c3))


# ---------------------------------------------------------------------------
# Task 5.2 — ``clear_client_cache``
# ---------------------------------------------------------------------------
#
# ``clear_client_cache`` (``server.py:330-332``) drops every cached client via
# ``_CLIENT_CACHE.clear()``. Task 5.2 requires: after clearing, subsequent
# ``_client`` calls create new instances instead of returning cached ones.
# The tests below also cover the empty-cache no-op and post-clear refill,
# which are the two regressions a broken ``clear`` could introduce (raising on
# an empty cache, or leaving the cache unable to accept new entries).


def test_clear_client_cache_empties_the_cache() -> None:
    """``clear_client_cache()`` removes every cached client so
    ``len(_CLIENT_CACHE) == 0``.

    After populating the cache with two distinct clients, calling
    ``clear_client_cache()`` must leave it empty. This is the documented
    contract (``server.py:330-332``: ``_CLIENT_CACHE.clear()``) and the
    foundation for the isolation fixture above.
    """
    _client(_BASE_A, _KEY_A)
    _client(_BASE_B, _KEY_B)
    assert len(_CLIENT_CACHE) == 2

    clear_client_cache()

    assert len(_CLIENT_CACHE) == 0
    assert _CLIENT_CACHE == {}


def test_clear_client_cache_subsequent_calls_create_new_instances() -> None:
    """After clearing, a subsequent ``_client(base_url, api_key)`` call
    constructs a new instance rather than returning the previously cached one.

    This is the behavioral consequence of clearing (task 5.2's requirement):
    the old instance is no longer reachable via the cache, so the same
    arguments produce a cache miss and a fresh ``Mem0OSSClient``. The new
    instance must not be the same object as the one returned before the clear
    (``is not``).
    """
    before = _client(_BASE_A, _KEY_A)

    clear_client_cache()

    after = _client(_BASE_A, _KEY_A)

    assert before is not after
    assert isinstance(after, Mem0OSSClient)
    # The cache holds exactly the one new instance.
    assert len(_CLIENT_CACHE) == 1
    assert next(iter(_CLIENT_CACHE.values())) is after


def test_clear_client_cache_is_idempotent() -> None:
    """Calling ``clear_client_cache()`` on an already-empty cache is a no-op
    (no error, cache stays empty).

    ``dict.clear()`` is idempotent, so the wrapper is too. This guards against
    a regression that wrapped the clear in a conditional that raised on an
    empty cache.
    """
    clear_client_cache()
    clear_client_cache()

    assert len(_CLIENT_CACHE) == 0


def test_clear_client_cache_then_refill_works() -> None:
    """After clearing, the cache can be refilled and resumes normal caching
    behavior (identity holds for repeated calls).

    This verifies the cache is not left in a broken state after clearing —
    a regression that, e.g., replaced the dict object with a sentinel or
    disabled insertion would fail here.
    """
    _client(_BASE_A, _KEY_A)
    clear_client_cache()

    first = _client(_BASE_A, _KEY_A)
    second = _client(_BASE_A, _KEY_A)

    assert first is second
    assert len(_CLIENT_CACHE) == 1
