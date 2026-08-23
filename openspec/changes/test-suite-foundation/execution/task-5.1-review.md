# Task 5.1 Review — test_client_cache.py (Task 5.1 additions)

Reviewer: code-reviewer
Date: 2026-08-23
File: tests/unit/test_client_cache.py (lines 1-285, the 5.1 portion)
Commit: f8a77aa688ace889dd3f4a30dd29878b81def7e0

## Summary

PASS. Task 5.1 correctly implements the `_client` cache test coverage
required by the spec: identity (same args -> same instance via `is`),
distinct instances for different `api_key`/`base_url`, cache population
side effect, and FIFO eviction with `_CLIENT_CACHE_MAX` monkeypatched to
a small value. The autouse `_reset_client_cache` fixture clears the
module-level `_CLIENT_CACHE` before and after each test, isolating the
shared mutable state. All claims traced against `server.py:314-327` and
verified empirically.

## Findings

### CRITICAL
None.

### HIGH
None.

### MEDIUM
None.

### LOW
- [L1] `test_client_does_not_evict_when_below_max` (lines 252-284) fills
  the cache to *exactly* `max` (3 entries, max=3). The docstring
  acknowledges this does not distinguish `>=` from `>` at `len < max`,
  and explicitly defers that distinction to
  `test_client_cache_never_exceeds_max_across_many_inserts`. The
  self-awareness makes this acceptable, but the test name slightly
  oversells what it proves. No action needed.
- [L2] The autouse fixture is module-local (not in `conftest.py`). This
  is correct and intended — only this module mutates `_CLIENT_CACHE`
  directly — but a future test file that also touches the cache would
  need to duplicate the fixture. Acceptable for now; revisit if a second
  cache-touching module appears.

## Verification commands run

- `python -m pytest tests/unit/test_client_cache.py -q`:
  `61 passed in 0.70s` (5.1 + 5.2 combined; all green)
- `python -m ruff check tests/unit/test_client_cache.py`:
  `All checks passed!`
- `python -m mypy tests/unit/test_client_cache.py`:
  `Success: no issues found in 2 source files`

## Spec coverage matrix

| Spec requirement (tasks.md 5.1) | Test name | Covered? | Notes |
|---|---|---|---|
| same (base_url, api_key) -> same instance (`is`) | test_client_returns_same_instance_for_same_base_url_and_api_key | YES | line 102, `is` not `==` |
| different api_key -> different instance | test_client_returns_distinct_instance_for_different_api_key | YES | line 119 |
| different base_url -> different instance | test_client_returns_distinct_instance_for_different_base_url | YES | line 136 |
| eviction: monkeypatch _CLIENT_CACHE_MAX to small value | test_client_evicts_oldest_entry_when_cache_full | YES | max=2, line 187 |
| oldest key dropped | test_client_evicts_oldest_entry_when_cache_full | YES | line 205, `_BASE_A` absent |
| len never exceeds max | test_client_cache_never_exceeds_max_across_many_inserts | YES | max=3, 10 inserts, assert after each, line 241 |
| clear_client_cache() in fixture before+after each test | _reset_client_cache (autouse) | YES | lines 66-78 |

## Detailed analysis

### Correctness vs implementation
Traced `server.py:318-327`:
- Cache key `(base_url, sha256(api_key)[:16])` — line 319. The tests use
  distinct `_KEY_A`/`_KEY_B` and `_BASE_A`/`_BASE_B` so both key
  components are exercised. `test_client_populates_cache_after_first_call`
  (line 158) asserts `key[0] == _BASE_A` (base_url verbatim), confirming
  the key shape.
- Cache hit returns stored instance directly (line 321-322) — pinned by
  `is` assertion at line 102.
- Eviction branch `if len(_CLIENT_CACHE) >= _CLIENT_CACHE_MAX: pop(next(iter))`
  (lines 323-324) — FIFO by dict insertion order. The max=2 test (line
  187) and the max=3 invariant test (line 237) both exercise this.
- Insert after construction (line 326) — pinned by
  `test_client_populates_cache_after_first_call` asserting
  `len(_CLIENT_CACHE) == 1` and `stored is client`.

### Eviction logic
`test_client_evicts_oldest_entry_when_cache_full` (lines 166-222):
- Fills cache to 2 (c1=_BASE_A, c2=_BASE_B), asserts full.
- Inserts c3=base_c -> evicts _BASE_A (oldest).
- Asserts `len == 2`, `_BASE_A` absent, surviving keys `[_BASE_B, base_c]`
  in insertion order. Correct: dict preserves insertion order, `pop(next(iter))`
  removes the first-inserted.
- Then re-inserts _BASE_A (c1_again) -> evicts _BASE_B (now oldest).
  Asserts `c1_again is not c1` (fresh construction on miss) and
  `_BASE_B` absent. Correct.
- All three clients distinct. Correct.

`test_client_cache_never_exceeds_max_across_many_inserts` (lines 225-249):
- max=3, 10 inserts, asserts `len <= max` after *every* insert. This is
  the test that distinguishes `>=` from `>`: a `>` regression would let
  len reach 4 before evicting. Final surviving ports `[9007, 9008, 9009]`
  confirm FIFO (last 3 of 10). Correct.

### Cache isolation
`_reset_client_cache` (lines 66-78) is autouse, clears before+after.
`clear_client_cache()` calls `_CLIENT_CACHE.clear()` (server.py:332),
which mutates the dict in place — so the module-imported reference
(`from server import _CLIENT_CACHE`) stays valid. No stale-reference
risk. The fixture is the foundation for the 5.2 tests as well.

### Why monkeypatch max instead of 32 real clients
The docstring (lines 30-39) explains: 32 real `Mem0OSSClient`
instances each construct a `requests.Session`, exercising nothing the
eviction branch doesn't already exercise at max=2. Correct trade-off —
fast, deterministic, same branch coverage.

### Style consistency
Consistent with the rest of the unit suite: long Google-style docstrings
citing `server.py` line numbers, section header comments, deterministic
local-URL/key fixtures, `monkeypatch: pytest.MonkeyPatch` typed
parameter. No emojis, no `# type: ignore` needed (no Pydantic
construction here).

## Verdict

- [x] PASS — no actionable findings
- [ ] PASS WITH FINDINGS — minor findings, fix recommended
- [ ] FAIL — critical/high findings must be fixed before commit
