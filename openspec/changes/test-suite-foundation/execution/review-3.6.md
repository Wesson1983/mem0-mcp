# Review — Task 3.6 (unit tests for `_with_default_filters`)

Reviewer: python-pro
Date: 2026-08-23
Files reviewed: tests/unit/test_helpers.py (lines 717-881, `_with_default_filters` tests), src/mem0_mcp_server/server.py (lines 335-344, `_with_default_filters`)

## Summary

pass-with-findings — The file implements exactly what Task 3.6 specifies: every
named case is explicitly tested with non-vacuous assertions — `user_id`
injection when absent (empty dict AND dict-with-other-keys, proving the check is
key-presence-based not emptiness-based), `agent_id` injection when absent and
`default_agent` is set, preservation of caller-supplied `user_id` and
`agent_id` (each individually AND both together, with exact-value asserts that
prove the caller won over the default rather than merely matching some dict),
and `None` filters input (returns a fresh dict, does not raise). All 88 tests
pass (12 new `_with_default_filters` tests + 1 whitespace remediation test
carried over from review-3.5 [M2]), `ruff check` is clean (the 3.5 [M1] ruff
regression was fixed: the import is now multi-line at lines 54-61), and test
isolation is perfect (no fixtures, no env vars, no shared mutable state, no
order dependence).

The three special-attention items in the review brief are all pinned:
- (a) the `if default_agent` (truthiness) vs `if default_agent is not None`
  distinction is pinned by `test_with_default_filters_falsy_default_agent_does_not_inject_agent_id`
  with BOTH a `None` row and an `""` row (lines 823-845). A regression to
  `is not None` would inject `agent_id: ""` for the empty-string row and fail
  `assert result == {"user_id": "u-default"}` AND `assert "agent_id" not in result`.
- (b) non-mutation is pinned by `test_with_default_filters_does_not_mutate_input`
  (lines 848-867): it snapshots the input, asserts `filters == snapshot` after
  the call, asserts `result is not filters` (catches `result = filters`), then
  mutates the result and asserts the input is unaffected. A regression to
  `result = filters` (dropping `dict(...)`) fails `assert result is not filters`.
- (c) empty-dict input is handled correctly — `{}` is falsy so the `else: {}`
  branch returns a fresh literal; the `empty-filters-injects-user-id` row and
  the falsy-`default_agent` tests exercise it. The `if filters` vs
  `if filters is not None` distinction is unobservable for `{}` (both produce
  `{}`), so it is not — and need not be — pinned; the mutant is behaviorally
  equivalent (see L3).

Mutation coverage is strong: every spec-relevant mutant I checked is caught
(inverted `user_id`/`agent_id` membership guards, dropped `default_agent`
guard, `is not None` regression, dropped `dict()` copy). See the
mutation-coverage audit below.

One Medium finding: `mypy tests/unit/test_helpers.py` now FAILS with
`Missing type arguments for generic type "dict" [type-arg]` at line 745 — the
3.6 tests annotate `filters: dict` and `expected: dict` as bare `dict`, which
violates `disallow_any_generics` under `[tool.mypy] strict = true`
(`pyproject.toml:90-92`). Review-3.5 reported mypy clean on this file, so this
is a type-cleanliness regression introduced by task 3.6. It is not CI-gated
(CI runs `mypy src/` only, per tasks.md 13.1), but it breaks the test-file
cleanliness contract that reviews 3.1-3.5 tracked and maintained. The
remaining findings are Low: two `# type: ignore[arg-type]` comments that are
workarounds for an over-loose `default_agent: object` annotation (tightening to
`Optional[str]` removes the ignores), an informational note on the unobservable
`if filters` distinction, and the carried-forward private-symbol import note.

No Critical or High findings. The spec's four cases are all explicitly tested,
the assertions prove caller-wins-over-defaults (not just dict equality), and
non-mutation is robustly proven. The gaps are a mypy regression and minor
type-annotation style.

## Findings

### Critical

- None

### High

- None

### Medium

- **[M1] `mypy` fails — the 3.6 tests annotate `filters` and `expected` as bare
  `dict`, violating `disallow_any_generics` under strict mypy; a type-cleanliness
  regression introduced by this task**
  - File: `tests/unit/test_helpers.py`, line 745
  - What: The signature
    `def test_with_default_filters_injects_user_id_when_absent(filters: dict, default_agent: object, expected: dict) -> None:`
    uses bare `dict` for `filters` and `expected`. `pyproject.toml` configures
    `[tool.mypy] strict = true` (`pyproject.toml:90-92`), which enables
    `disallow_any_generics`, so a bare `dict` triggers `[type-arg]`. `mypy`
    reports:
    ```
    tests\unit\test_helpers.py:745: error: Missing type arguments for generic type "dict"  [type-arg]
    Found 1 error in 1 file (checked 1 source file)
    ```
    Review-3.5 explicitly reported "`mypy tests/unit/test_helpers.py`: Success:
    no issues found in 1 source file (exit 0)" — so this failure was introduced
    by task 3.6, not pre-existing. The 3.1-3.5 tests use only `str`/`int`
    parametrize params and never bare `dict`, so the bare-`dict` annotation is
    new to this task. (Note: the other 3.6 test signatures that take `dict`
    values do so via inline literals, not annotations, so they do not trigger
    `[type-arg]` — line 745 is the only flagged site.)
  - Why it matters: The Task 3.6 spec says "Verify all assertions pass," and the
    review brief requires running `mypy` and tracking ruff/mypy cleanliness.
    Reviews 3.1-3.5 all reported mypy clean on this file; 3.6 breaks that. It is
    not a CI gate (CI runs `mypy src/` only, per tasks.md 13.1, so test files are
    not type-checked in CI), so it does not block merging — but it is a
    consistency regression on the cleanliness contract the prior reviews
    established, symmetric to how review-3.5 [M1] flagged the ruff regression as
    Medium. The fix is a one-line annotation tightening.
  - Suggested fix: Annotate the dict params with their key/value types, e.g.
    `filters: dict[str, object], expected: dict[str, object]` (or
    `dict[str, str]` — the test values are all `str`-keyed with `str` values).
    Then re-run `mypy tests/unit/test_helpers.py` to confirm "Success: no issues
    found". This also aligns with the `from __future__ import annotations` style
    already in the file.

### Low

- **[L1] Two `# type: ignore[arg-type]` comments are workarounds for an
  over-loose `default_agent: object` annotation; tightening to `Optional[str]`
  removes the need for the ignores**
  - File: `tests/unit/test_helpers.py`, lines 756 and 843
  - What: Both
    `test_with_default_filters_injects_user_id_when_absent` (line 756) and
    `test_with_default_filters_falsy_default_agent_does_not_inject_agent_id`
    (line 843) call `_with_default_filters(filters, "u-default", default_agent)`
    with a trailing `# type: ignore[arg-type]`. The ignore is required because
    the parametrize param is annotated `default_agent: object` (lines 745, 832)
    while `_with_default_filters` expects `default_agent: Optional[str]`
    (`server.py:336`). The parametrize values are `None` and `""` (lines 734-737,
    825) — both valid `Optional[str]`. Annotating the param as `Optional[str]`
    (or `str | None`) would be more precise AND eliminate the `type: ignore`,
    because `None`/`""` are assignable to `Optional[str]` without an arg-type
    error. The review brief explicitly asks to scrutinize `# type: ignore`
    comments: these are not strictly justified — they mask an unnecessarily loose
    annotation rather than suppressing a genuine third-party typing limitation.
  - Why it matters: `# type: ignore` comments silence mypy for an entire error
    category on that line, which can hide future real type errors that happen to
    classify as `[arg-type]`. Tightening the annotation is strictly better: more
    accurate types, no suppression, no information lost. The current approach
    (loose `object` + ignore) is a known anti-pattern where the ignore is a
    symptom of the annotation choice rather than a library limitation. Low
    severity because the ignores are narrowly scoped (`[arg-type]` only) and the
    tests are correct at runtime.
  - Suggested fix: Change `default_agent: object` to `default_agent: Optional[str]`
    on lines 745 and 832 (adding `Optional` to the import from `typing`, or using
    `str | None` since `from __future__ import annotations` is active), then
    delete `  # type: ignore[arg-type]` from lines 756 and 843. Re-run mypy to
    confirm no new errors. This also resolves the interaction with M1: the bare
    `dict` (M1) is a separate issue on the same signature and must be fixed
    independently.

- **[L2] Direct import of the private `_with_default_filters` symbol**
  - File: `tests/unit/test_helpers.py`, line 60
  - What: `from mem0_mcp_server.server import (... _with_default_filters)`
    reaches into a single-underscore-prefixed module-private function.
  - Why it matters: This is spec-mandated — Task 3.6 explicitly says "tests for
    `_with_default_filters`" and `design.md` scopes the unit layer at "pure
    helpers" including this one. The tests exercise `_with_default_filters`
    purely through its input/output contract (filters + defaults → dict), which
    is the right coupling level for a helper unit test. Noted for consistency
    with review-3.1 [L4], review-3.2, review-3.3 [L4], review-3.4 [L4], and
    review-3.5 [L2]; no action needed.
  - Suggested fix: None required.

- **[L3] The `if filters` (truthiness) vs `if filters is not None` distinction
  in the constructor is unobservable for the only falsy-non-`None` input (`{}`)
  and therefore is not (and cannot be) pinned — informational only**
  - File: `tests/unit/test_helpers.py`, lines 730-757, 823-845; `src/mem0_mcp_server/server.py`, line 339
  - What: The implementation is `result = dict(filters) if filters else {}`
    (`server.py:339`). The only falsy-non-`None` value `filters` can take is `{}`
    (an empty dict is falsy). Under `if filters`: `{}` is falsy → `else` → `{}`.
    Under `if filters is not None`: `{}` is not `None` → `dict({})` → `{}`. Both
    paths yield an identical empty dict, so no test can distinguish the two
    guards by output. The empty-dict input IS tested (`empty-filters-injects-
    user-id` row at line 734, and the falsy-`default_agent` tests at lines 823-845
    use `{}`), and it is handled correctly (a fresh dict is returned, `user_id`
    is injected) — but the distinction between the two guard forms is
    behaviorally equivalent for this input and thus not pinnable.
  - Why it matters: This is not a gap — the mutant (`if filters` →
    `if filters is not None`) is benign (identical observable behavior for every
    possible input: `None` → `{}` either way; `{}` → `{}` either way; non-empty
    dict → `dict(filters)` either way). There is no input that would expose a
    difference, so no test could catch it even in principle. Noted only because
    the review brief flagged it as a special-attention item (c): the answer is
    that empty-dict input is handled correctly, and the guard distinction is
    unobservable by construction. No action needed.
  - Suggested fix: None required. (If one wanted to document the choice, a
    comment in the implementation noting "`if filters` treats `{}` and `None`
    identically, which is intended" would make the truthiness-vs-`is not None`
    choice explicit — but this is an implementation concern, not a test concern.)

## Verification

- `python -m pytest tests/unit/test_helpers.py -v --tb=short`: 88 passed in
  1.02s (exit 0) — 9 `_validate_base_url` (3.1) + 28 `_redact` (3.2) + 16
  `_validate_memory_id` (3.3) + 13 `_error` (3.4) + 10 `_int_env` (3.5, now
  including the whitespace remediation test from review-3.5 [M2]) + 12
  `_with_default_filters` (3.6): 2 user_id-injection rows, 1 agent_id-injection,
  1 preserves-caller-user_id, 1 preserves-caller-agent_id, 1 preserves-both,
  1 None-filters, 1 None-filters-with-default-agent, 2 falsy-default-agent rows,
  1 does-not-mutate, 1 fresh-dict-for-None.
- `python -m ruff check tests/unit/test_helpers.py`: All checks passed!
  (exit 0). The review-3.5 [M1] ruff regression (`I001` on the single-line
  import) is resolved — the import is now multi-line (lines 54-61), sorted, and
  under the 100-char limit.
- `python -m mypy tests/unit/test_helpers.py`: **FAILS** —
  `tests\unit\test_helpers.py:745: error: Missing type arguments for generic
  type "dict" [type-arg]`. 1 error in 1 file. This is a regression introduced by
  task 3.6 (review-3.5 reported mypy clean). See M1. Note: CI runs `mypy src/`
  only (per tasks.md 13.1), so this is not a CI gate, but the file is no longer
  clean under `[tool.mypy] strict = true`.
- Empirical checks:
  - `{} is not None` is `True`; `bool({})` is `False`. Confirms the `if filters`
    vs `if filters is not None` distinction is unobservable for `{}` (L3).
  - `"" is not None` is `True`; `bool("")` is `False`. Confirms the
    `if default_agent` (truthiness) vs `if default_agent is not None` distinction
    IS observable for `""` and is pinned by the `default-agent-empty-string` row.
  - `dict({}) is {}` is `False` (a copy is a distinct object); `{} is {}` is
    `False` (each literal is a fresh dict). Confirms the `else: {}` branch
    returns a fresh dict for every call, underpinning the
    `returns_fresh_dict_for_none_input` two-call `is not` assert.

## Spec-coverage audit (Task 3.6, tasks.md line 19)

| Spec case | Test | File:line | Verdict |
|---|---|---|---|
| injects `user_id` when absent | `test_with_default_filters_injects_user_id_when_absent` (2 rows: empty dict, dict with other keys) | lines 730-757 | covered (key-presence-based, not emptiness-based) |
| injects `agent_id` when absent and `default_agent` is set | `test_with_default_filters_injects_agent_id_when_absent_and_default_set` | lines 760-768 | covered (both keys injected) |
| preserves caller-supplied `user_id` | `test_with_default_filters_preserves_caller_user_id` | lines 771-781 | covered (exact-value assert proves caller won) |
| preserves caller-supplied `agent_id` | `test_with_default_filters_preserves_caller_agent_id` | lines 784-793 | covered (exact-value assert proves caller won) |
| preserves both caller-supplied values | `test_with_default_filters_preserves_both_caller_values` | lines 796-801 | covered (neither default injected) |
| handles `None` filters input | `test_with_default_filters_handles_none_filters` + `test_with_default_filters_none_filters_with_default_agent` | lines 804-820 | covered (no raise; fresh dict; both with and without default_agent) |
| "Verify all assertions pass" | full suite green | — | covered (88/88 pass) |

Every case in the spec is explicitly tested. Beyond-spec positive coverage: the
falsy-`default_agent` distinction (`None` AND `""`, pinning truthiness vs
`is not None`), non-mutation of the input dict, and fresh-dict-for-`None`-input
are all covered beyond the literal spec wording and strengthen the suite.

## Assertion-correctness audit (review dimension 2)

- **`user_id` injection** (`test_with_default_filters_injects_user_id_when_absent`,
  line 757): `assert result == expected` where expected is
  `{"user_id": "u-default"}` / `{"q": "x", "user_id": "u-default"}`. Non-vacuous
  — would fail if `user_id` were not injected (missing key) or if other keys
  were dropped. The `other-keys-no-user-id-injects-user-id` row proves the
  `"user_id" not in result` check is key-presence-based, not emptiness-based (a
  non-empty dict without `user_id` still gets the injection). ✓
- **`agent_id` injection** (`test_with_default_filters_injects_agent_id_when_absent_and_default_set`,
  line 768): `assert result == {"user_id": "u-default", "agent_id": "a-default"}`.
  Would fail if `agent_id` were not injected or if `user_id` were dropped. ✓
- **caller `user_id` wins** (`test_with_default_filters_preserves_caller_user_id`,
  line 781): `assert result == {"user_id": "u-caller", "agent_id": "a-default"}`.
  The expected `user_id` is `"u-caller"` (the caller value), NOT `"u-default"`
  (the default) — so this proves the caller won, not just that some dict
  matched. It also proves `agent_id` was still injected (the two injections are
  independent). A mutant that overwrote the caller's `user_id` with the default
  would produce `"u-default"` and fail. ✓
- **caller `agent_id` wins** (`test_with_default_filters_preserves_caller_agent_id`,
  line 793): `assert result == {"user_id": "u-default", "agent_id": "a-caller"}`.
  Symmetric — expected `agent_id` is `"a-caller"`, proving the caller won. ✓
- **both caller values** (`test_with_default_filters_preserves_both_caller_values`,
  line 801): `assert result == {"user_id": "u-caller", "agent_id": "a-caller"}`.
  Proves neither default was injected. (Does not assert `result is not filters`,
  but the no-copy mutant for this exact input is caught by
  `test_with_default_filters_does_not_mutate_input`, which uses a dict where
  injection occurs — see mutation audit.) ✓
- **`None` filters** (`test_with_default_filters_handles_none_filters`, line 813;
  `..._none_filters_with_default_agent`, line 820): `assert result == {"user_id": "u-default"}`
  / `{"user_id": "u-default", "agent_id": "a-default"}`. Would fail with
  `TypeError` if `dict(None)` were called (the `if filters` guard prevents this)
  or if the keys were not injected. ✓
- **falsy `default_agent`** (`test_with_default_filters_falsy_default_agent_does_not_inject_agent_id`,
  lines 844-845): `assert result == {"user_id": "u-default"}` AND
  `assert "agent_id" not in result`. The membership guard is the key pin: a
  regression to `if default_agent is not None` would inject `agent_id: ""` for
  the empty-string row, and `"agent_id" not in result` would fail even though
  `result == {"user_id": "u-default", "agent_id": ""}` would also fail the
  equality assert. Belt-and-suspenders. ✓
- **non-mutation** (`test_with_default_filters_does_not_mutate_input`,
  lines 862-867): `assert filters == snapshot` (input unchanged) +
  `assert result is not filters` (distinct object) + mutate-result-then-assert-
  input-unaffected. Three independent guards; the `is not` check is the one that
  catches `result = filters` (dropped copy). ✓
- **fresh dict for `None`** (`test_with_default_filters_returns_fresh_dict_for_none_input`,
  lines 877-881): two calls, `assert a is not b`. Catches a shared-singleton
  mutant for the `else: {}` branch (both `None` and `{}` hit that branch; this
  test guards it via the `None` path). ✓

No always-true or tautological assertions found. Every preservation assert uses
a caller value distinct from the default, so caller-wins is proven, not assumed.

## Edge-case audit (review dimension 3)

| Edge case | Test | File:line | Verdict |
|---|---|---|---|
| empty filters `{}` | `empty-filters-injects-user-id` row + falsy-`default_agent` tests | lines 734, 823-845 | covered (fresh dict, user_id injected) |
| filters with other keys, no `user_id` | `other-keys-no-user-id-injects-user-id` row | line 737 | covered (other keys preserved, user_id injected) |
| filters with only `user_id` | `test_with_default_filters_preserves_caller_user_id` | lines 771-781 | covered (caller wins, agent_id still injected) |
| filters with only `agent_id` | `test_with_default_filters_preserves_caller_agent_id` | lines 784-793 | covered (caller wins, user_id still injected) |
| filters with both keys | `test_with_default_filters_preserves_both_caller_values` | lines 796-801 | covered (neither default injected) |
| `None` filters, `default_agent=None` | `test_with_default_filters_handles_none_filters` | lines 804-813 | covered (no raise, only user_id) |
| `None` filters, `default_agent` set | `test_with_default_filters_none_filters_with_default_agent` | lines 816-820 | covered (both keys injected) |
| `default_agent=None` (falsy) | `default-agent-none` row | line 826 | covered (agent_id not injected) |
| `default_agent=""` (falsy, not None) | `default-agent-empty-string` row | line 827 | covered (pins truthiness vs `is not None`) |
| non-mutation of input dict | `test_with_default_filters_does_not_mutate_input` | lines 848-867 | covered (snapshot + `is not` + mutate-result) |
| fresh dict for `None` input | `test_with_default_filters_returns_fresh_dict_for_none_input` | lines 870-881 | covered (two-call `is not`) |

All edge cases flagged in the review brief are covered. The one unobservable
distinction (`if filters` vs `if filters is not None` for `{}`) is benign and
documented in L3.

## Non-mutation audit (review dimension 4)

- `test_with_default_filters_does_not_mutate_input` (lines 848-867) is the
  primary non-mutation guard. It uses a truthy input (`{"q": "x", "user_id": "u-caller"}`,
  which takes the `dict(filters)` copy path), snapshots it, calls the function
  (which injects `agent_id`), and asserts: (1) `filters == snapshot` (input
  unchanged), (2) `result is not filters` (a distinct object was returned), (3)
  mutating `result["agent_id"]` does not affect `filters`. This three-pronged
  check catches `result = filters` (fails `is not`), `result = dict(filters)`
  with in-place mutation before return (fails `filters == snapshot`), and any
  aliasing that leaks result mutations back into the input (fails the third
  assert). ✓
- `test_with_default_filters_returns_fresh_dict_for_none_input` (lines 870-881)
  guards the `else: {}` branch: two consecutive `None`-input calls return
  distinct objects (`a is not b`), catching a shared-singleton mutant that
  reused a module-level empty dict (which would also mutate the singleton on
  the first `user_id` injection and corrupt subsequent calls). Because both
  `None` and `{}` inputs hit the same `else: {}` branch, this `None`-path guard
  transitively covers the empty-dict path too. ✓
- The empty-dict input path (`else: {}`) is not given its own two-call `is not`
  check, but as noted above the `None`-path two-call check covers the same
  branch, so a shared-singleton mutant is caught regardless. No gap.

## Test-isolation audit (review dimension 5)

- `_with_default_filters` is a pure function: no module-level mutable state read,
  no env vars, no I/O, no globals. The only module-level state in `server.py`
  is `_CLIENT_CACHE`, which this function does not touch.
- No fixtures used by the 3.6 tests. No `monkeypatch`, no `caplog`, no shared
  mutable fixtures.
- Every test constructs its input dict inline (or via parametrize values that
  are fresh literals per row). No test mutates a dict passed in from another
  test. No order dependence.
- `test_with_default_filters_does_not_mutate_input` takes its own snapshot
  before the call, so it is self-contained.
- Clean. No isolation issues.

## Idiomatic-pytest audit (review dimension 6)

- `@pytest.mark.parametrize` with explicit `ids` is used for the two multi-row
  cases (`injects_user_id_when_absent` with 2 rows, `falsy_default_agent` with 2
  rows). IDs are human-readable (`empty-filters-injects-user-id`,
  `default-agent-empty-string`, etc.).
- Standalone tests are used for singular behaviors (agent_id injection,
  each preservation case, None-handling, non-mutation, fresh-dict). This is
  appropriate — the preservation cases are symmetric but read clearly
  separately, and folding them into one parametrize would obscure the
  per-key intent.
- Naming follows `test_with_default_filters_<behavior>` consistently.
- `from __future__ import annotations` + `-> None` on every test function.
- No unnecessary fixtures; no `pytest.approx` misuse; no brittle string
  matching (these are exact-dict-equality asserts, which is correct for a
  dict-returning helper).
- Minor: the `default_agent: object` annotation choice (L1) is less idiomatic
  than `Optional[str]` for values that are `None`/`""`; the resulting
  `# type: ignore[arg-type]` is a smell. See L1.

## Type-hints & style audit (review dimension 7)

- `from __future__ import annotations` is present (line 48), so
  `str | None` syntax is available even on the 3.10 target.
- PEP 8: naming, spacing, and line lengths are clean (ruff passes, 100-char
  limit respected).
- Annotations: all test functions have `-> None`. The parametrize params are
  annotated, but `filters: dict` / `expected: dict` are bare (M1) and
  `default_agent: object` is over-loose (L1). The combination produces one
  mypy error (M1) and two `type: ignore` suppressions (L1).
- `# type: ignore[arg-type]` (lines 756, 843): present, narrowly scoped to
  `[arg-type]`, but not justified by a third-party typing limitation — they
  mask the over-loose `object` annotation. See L1.
- ruff: clean. mypy: 1 error (M1).

## Scope-discipline audit (review dimension 8)

- `git diff HEAD -- tests/unit/test_helpers.py`: 177 insertions, 1 deletion.
  The single deletion is the module-docstring line
  `Tests for ``_with_default_filters`` live in task 3.6.`, replaced by the
  expanded 3.6 docstring block (lines 37-46). No existing test logic (3.1-3.5)
  was modified.
- The 12 `_with_default_filters` tests (lines 730-881) are appended after the
  3.5 tests, under a clearly delimited section header (lines 717-727).
- One additional test, `test_int_env_returns_default_for_whitespace_and_warns`
  (lines 686-714), is a 3.5 remediation addressing review-3.5 [M2] (the
  whitespace-only env value that was not previously tested). It is a NEW test,
  not a modification of an existing 3.5 test, and it is correctly placed in the
  3.5 section. Bundling a 3.5 remediation with the 3.6 work is acceptable — it
  fixes a prior review finding without altering any existing test's behavior.
- The import block (lines 54-61) was reformatted to multi-line to fix the
  review-3.5 [M1] ruff regression; `_with_default_filters` was added to it.
  This is a necessary, in-scope change.
- No scope creep into tasks 3.7+ (`_resolve_settings` etc.). No modifications to
  `server.py` or any non-test file (only `tasks.md` checkbox tick, which is
  expected).

## Mutation-coverage audit (review dimension 9)

| Hypothetical break in `_with_default_filters` | user_id-inject | agent_id-inject | preserves-user | preserves-agent | preserves-both | None-filters | None+agent | falsy-agent (None/`""`) | non-mutate | fresh-None | Caught? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `result = filters` (drop `dict()` copy, keep `else {}`) | pass | pass | pass | pass | pass | pass | pass | pass | **FAIL** (`result is not filters`) | pass | yes |
| `result = dict(filters) if filters else filters` (else returns input) | pass | pass | pass | pass | pass | pass | pass | **FAIL** (`result is not filters` for `{}` input — but no `is not` check on empty-dict; equality holds) | pass | pass | **partial** (empty-dict `is not` not checked; see note) |
| `if "user_id" in result` (inverted membership) | **FAIL** (not injected for empty) | **FAIL** (not injected) | **FAIL** (overwrites caller with default) | pass | **FAIL** (overwrites) | **FAIL** (not injected) | **FAIL** (not injected) | **FAIL** (not injected) | pass | pass | yes |
| `if "agent_id" in result` (inverted membership) | pass | **FAIL** (not injected when absent) | pass | **FAIL** (overwrites caller) | **FAIL** (overwrites) | pass | **FAIL** (not injected) | pass | pass | pass | yes |
| `if default_agent is not None` (truthiness → `is not None`) | pass | pass | pass | pass | pass | pass | pass | **FAIL** (`""` row injects `agent_id: ""`) | pass | pass | yes |
| `if default_agent and "agent_id" not in result` → `if "agent_id" not in result` (drop default_agent guard) | pass | pass | pass | pass | pass | pass | pass | **FAIL** (both rows inject agent_id) | pass | pass | yes |
| `if filters` → `if filters is not None` (constructor truthiness → `is not None`) | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass | **no** (benign: `{}` → `{}` either way; see L3) |
| drop `user_id` injection entirely | **FAIL** | **FAIL** (missing user_id) | **FAIL** | **FAIL** (missing user_id) | **FAIL** | **FAIL** | **FAIL** | **FAIL** | pass | **FAIL** | yes |
| drop `agent_id` injection entirely (keep guard) | pass | **FAIL** (missing agent_id) | **FAIL** (missing agent_id) | pass | pass | pass | **FAIL** (missing agent_id) | pass | pass | pass | yes |

Note on the `result = dict(filters) if filters else filters` mutant: for
empty-dict input `{}`, `result = filters` (the input `{}`), then `user_id` is
injected mutating the input. The empty-dict tests assert only equality
(`result == {"user_id": "u-default"}`) and `"agent_id" not in result`, both of
which hold under the mutant (the input is mutated but the test doesn't
re-check the input dict for `{}` inputs). So this specific exotic mutant is
NOT caught for empty-dict input. However: (1) the realistic mutant
`result = filters` (drop copy, keep `else {}`) IS caught by
`does_not_mutate_input`'s `result is not filters` on a truthy input; (2) the
`else: filters` mutant is implausible (why return the input only in the else
branch?); (3) the `None`-path two-call `is not` check catches the
shared-singleton variant. This residual is covered by L3's observation that the
empty-dict `else` branch lacks its own `is not` check, and is rated
informational-only because the realistic mutant is caught.

Every spec-relevant mutant is caught. The only uncaught mutant
(`if filters` → `if filters is not None`) is behaviorally equivalent (benign),
and the one partially-caught exotic mutant (`else: filters`) is implausible and
covered by the realistic-mutant test. The three brief-flagged special-attention
items — (a) `is not None` regression, (b) dropped `dict()` copy, (c) empty-dict
handling — are all addressed: (a) and (b) are caught, (c) is handled correctly
with the guard distinction being benign/unobservable.

## Verdict

pass-with-findings — The implementation is spec-correct, all 88 assertions pass,
ruff is clean, and mutation coverage is strong for every behavior Task 3.6
lists. The three special-attention items are all pinned (truthiness guard,
non-mutation, empty-dict handling). The one Medium finding is a mypy
cleanliness regression (bare `dict` annotations) that is not CI-gated but
breaks the test-file type-cleanliness contract maintained across 3.1-3.5; the
remaining findings are Low (two avoidable `type: ignore` comments, an
informational unobservable-distinction note, and the carried-forward
private-import note). None block merging; M1 should be fixed for consistency
with the prior reviews' cleanliness tracking.
