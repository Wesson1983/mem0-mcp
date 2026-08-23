# Review — Task 3.2 (unit tests for `_redact`)

Reviewer: python-pro
Date: 2026-08-23
Files reviewed: tests/unit/test_helpers.py (lines 90-249, `_redact` tests), src/mem0_mcp_server/server.py (lines 89-101, `_redact` + `_REDACT_PATTERNS`)

## Summary

pass-with-findings — The file implements every behavior Task 3.2 specifies:
`api_key` patterns (4 variants: `api_key=`, `api-key=`, `apikey=`, `API_KEY=`),
`token` patterns (`token=`, `token: `), `bearer` tokens (`Bearer `, `bearer `),
`authorization` headers (`Authorization: `, `authorization=`), truncation to a
custom and the default limit, at-limit non-truncation, and non-sensitive text
preservation. All 25 `_redact` tests pass (34 total in the file), `ruff check`
is clean, and `mypy` is clean. Mutation coverage is strong for each behavior in
isolation — removing a pattern, breaking case-insensitivity, changing the
`{20,}` threshold, or breaking the truncation slice are all caught.

The one non-Low finding is that redaction and truncation are tested only in
isolation, never in combination. The implementation redacts-then-truncates
(`server.py:99-101`); a truncate-then-redact refactor would leak partial
secrets (verified empirically: `_redact("x"*490 + "api_key=" + "a"*40,
limit=500)` yields `...api_key=[R` today but `...api_key=aa` if truncation
ran first — 2 chars of secret survive because truncation cuts the value below
the 20-char threshold so the regex no longer matches). No current test catches
this; every redaction test uses short outputs well under 500, and every
truncation test uses non-sensitive text. This is a security-relevant
robustness gap, graded Medium because the spec lists the two behaviors
separately and does not explicitly mandate testing their interaction.

No Critical or High findings. The remaining findings are Low: two character-
class gaps in the bearer/authorization value patterns, a vacuous empty-string
assertion, a redundant length assertion, a multi-secret test that covers 3 of
4 patterns, and an imprecise docstring comment about the quote-consuming
`["']?`.

## Findings

### Critical

- None

### High

- None

### Medium

- **[M1] Redaction and truncation are tested only in isolation, never in
  combination — a truncate-then-redact refactor would leak secrets and pass
  the suite**
  - File: `tests/unit/test_helpers.py`, lines 193-211 (truncation tests) and
    lines 100-166 (redaction tests)
  - What: Every redaction test (`test_redact_redacts_sensitive_patterns`,
    `test_redact_quoted_values_consume_opening_quote`,
    `test_redact_preserves_values_below_20_char_threshold`,
    `test_redact_redacts_multiple_secrets_in_one_string`,
    `test_redact_redacts_within_longer_text`) uses the default `limit=500`
    and produces output well under 500 chars. Every truncation test
    (`test_redact_truncates_to_custom_limit`,
    `test_redact_truncates_to_default_limit`,
    `test_redact_does_not_truncate_string_at_or_below_limit`) uses
    non-sensitive text (`"a"*100`, `"x"*600`, `"y"*500`). No test feeds
    `_redact` a sensitive pattern in a string long enough that truncation
    interacts with the redacted output.
  - The implementation (`server.py:99-101`) redacts first, then truncates:
    ```python
    for pat in _REDACT_PATTERNS:
        text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text[:limit]
    ```
    If a refactor reversed this to `return pat.sub(..., text[:limit])`, a
    secret near the end of a long string could be cut below the 20-char
    threshold by truncation, fail to match the regex, and survive in the
    output. Verified empirically:
    ```
    _redact("x"*490 + "api_key=" + "a"*40, limit=500)
      -> redact-first (current):  '...api_key=[R'   (marker truncated, secret gone)
      -> truncate-first (mutant): '...api_key=aa'   (2 chars of secret LEAKED)
    ```
    Both produce output of length 500; both pass every test in the file.
  - Why it matters: `_redact` is a security helper — its purpose is to
    prevent secrets from reaching logs. The order of operations is
    security-critical: redact-then-truncate guarantees no secret survives;
    truncate-then-redact does not. The current suite cannot distinguish the
    correct order from the insecure one. The spec (tasks.md line 15) lists
    "redacts ... truncates to the limit" as separate behaviors, so this is a
    robustness gap rather than a spec-coverage gap, but it is the kind of gap
    a redaction test suite should close.
  - Suggested fix: Add one test that combines a sensitive pattern with a
    `limit` small enough to truncate the redacted output, and one where the
    secret sits near the truncation boundary:
    ```python
    def test_redact_truncates_after_redaction() -> None:
        """Redaction runs before truncation: a secret near the limit is
        replaced, not partially exposed."""
        secret = "a" * 40
        text = "x" * 490 + f"api_key={secret}"
        result = _redact(text, limit=500)
        assert "a" * 20 not in result  # no 20-char run of the secret survives
        assert result.endswith("api_key=[R")  # marker is truncated, not the secret

    def test_redact_truncates_redacted_marker() -> None:
        """When the redacted output exceeds the limit, the [REDACTED] marker
        is truncated, not the original secret."""
        text = f"api_key={_SECRET}" + "z" * 600
        result = _redact(text, limit=20)
        assert _SECRET not in result
        assert len(result) == 20
    ```
    The first test fails under truncate-then-redact (the secret's 40 chars
    are cut to 10 by `[:500]`, the regex doesn't match, and `"a"*20` is not
    in the output — wait, that assertion would pass. Use
    `assert "api_key=aa" not in result` or assert the result contains
    `[REDACTED]` (possibly truncated). The key assertion is that no
    unredacted secret fragment appears where the marker should be.

### Low

- **[L1] Bearer and authorization value character classes (`.` and `=`) are
  not exercised**
  - File: `tests/unit/test_helpers.py`, line 97 (`_SECRET` definition)
  - What: `_SECRET = "abcdef0123456789abcdef0123456789"` is 32 chars of pure
    alphanumerics. The bearer regex (`server.py:92`) and authorization regex
    (`server.py:93`) allow `[A-Za-z0-9_\-\.=]` in the value (including `.` and
    `=`), while the api_key and token regexes (`server.py:90-91`) allow only
    `[A-Za-z0-9_\-]`. No test value contains `.`, `=`, `_`, or `-` in the
    secret portion. A regression that removed `\.=` from the bearer pattern
    or accidentally added it to the api_key pattern would pass undetected.
  - Why it matters: The spec does not require testing specific character
    classes, and the alphanumeric value proves the `{20,}` quantifier works.
    The gap is that the bearer/authorization patterns accept a wider
    character class than api_key/token, and that asymmetry is untested. Low
    risk because the spec does not mandate it and the current implementation
    is correct.
  - Suggested fix: Optionally add a parametrize row with a JWT-shaped bearer
    value (e.g. `Bearer eyJhbGciOiJIUzI1NiIsInR5c.` — contains `.`) to
    exercise the bearer-specific character class.

- **[L2] `test_redact_redacts_multiple_secrets_in_one_string` covers 3 of 4
  patterns**
  - File: `tests/unit/test_helpers.py`, lines 236-241
  - What: The test string is `f"api_key={_SECRET} and token={_SECRET} and
    Bearer {_SECRET}"` — it includes api_key (pattern 1), token (pattern 2),
    and bearer (pattern 3) but not authorization (pattern 4). The docstring
    (line 238) claims "the function iterates over every `_REDACT_PATTERNS`
    regex," but the test only proves 3 of 4 patterns fire in a single pass.
  - Why it matters: Each pattern is individually tested in
    `test_redact_redacts_sensitive_patterns`, so pattern 4 is not untested —
    it is just absent from the multi-secret composition test. The
    docstring's "every" claim is slightly over-stated relative to the test
    evidence. Low risk.
  - Suggested fix: Add `and Authorization: {_SECRET}` to the test string and
    the corresponding `and Authorization: [REDACTED]` to the expected output,
    or soften the docstring to "multiple sensitive patterns."

- **[L3] The empty-string assertion is vacuous — it passes under any
  implementation that does not raise**
  - File: `tests/unit/test_helpers.py`, line 233 (parametrize row
    `empty-string`, line 220)
  - What: `assert _redact("") == ""` is always true for any function that
    returns a string (slicing `""[:500]` is `""`, and `re.sub` on `""`
    returns `""`). It cannot distinguish a correct `_redact` from a no-op
    `lambda text, limit=500: text[:limit]` or even `lambda text, limit=500:
    ""`. The only value is proving no exception is raised on empty input.
  - Why it matters: The other non-sensitive-text rows (`greeting`,
    `pangram`, `ordinary-text`) are also `assert _redact(text) == text`,
    which in isolation cannot distinguish a correct function from a no-op —
    but the redaction tests provide the positive proof that `_redact` does
    transform sensitive input, so in context the non-sensitive tests are
    meaningful. The empty-string row adds no discriminating power beyond
    "does not crash on empty." Low risk; informational.
  - Suggested fix: None required. Optionally add a comment noting the
    empty-string row is a no-crash guard, not a behavior assertion.

- **[L4] Redundant `len(result) == 500` assertion in the default-limit test**
  - File: `tests/unit/test_helpers.py`, lines 204-205
  - What: `test_redact_truncates_to_default_limit` asserts both
    `assert len(result) == 500` and `assert result == "x" * 500`. The second
    implies the first (a string equal to `"x"*500` has length 500), making
    the `len` check redundant.
  - Why it matters: Harmless — redundant assertions do not weaken the test.
    Noted for style only. The custom-limit test (line 197) does not have
    this redundancy, so the two truncation tests are slightly asymmetric in
    style.
  - Suggested fix: Drop the `len(result) == 500` line, or keep it as an
    explicit boundary check with a comment. Either is fine.

- **[L5] Quoted-value docstring is imprecise about which `["']?` consumes
  the opening quote**
  - File: `tests/unit/test_helpers.py`, lines 146-147 (comment) and
    lines 159-165 (docstring)
  - What: The regex `(api[_-]?key["']?\s*[:=]\s*)["']?[A-Za-z0-9_\-]{20,}`
    contains two `["']?` quantifiers — one inside group 1 (before the
    separator) and one outside (after the separator, before the value). The
    comment and docstring say "the opening quote is consumed by the optional
    `["']?` in the regex," but do not specify which one. For the test input
    `api_key="..."`, the quote after `=` is consumed by the second `["']?`
    (outside group 1); the first `["']?` (inside group 1, before the
    separator) matches empty because there is no quote between `api_key` and
    `=`.
  - Why it matters: The test behavior is correct and the assertion is
    accurate (`api_key=[REDACTED]"` — opening quote consumed, closing quote
    survives). The imprecision is in the explanation, not the test. A reader
    who misidentifies which `["']?` fires might write a wrong test for the
    pre-separator quote case (e.g. `"api_key"=value`), which is untested.
    Low risk.
  - Suggested fix: Clarify the comment to "the opening quote after the
    separator is consumed by the second `["']?` (outside the capture
    group)."

## Verification

- `python -m pytest tests/unit/test_helpers.py -v --tb=short`: 34 passed in
  0.74s (exit 0) — 9 `_validate_base_url` tests (task 3.1) + 25 `_redact`
  tests (task 3.2): 10 redacts-sensitive-patterns, 2 quoted-values, 4
  below-threshold-preserves, 1 custom-limit-truncates, 1 default-limit-
  truncates, 1 at-limit-no-truncate, 4 non-sensitive-unchanged, 1 multiple-
  secrets, 1 within-longer-text.
- `python -m ruff check tests/unit/test_helpers.py`: All checks passed!
  (exit 0).
- `python -m mypy tests/unit/test_helpers.py`: Success: no issues found in 1
  source file (exit 0). Note: CI runs `mypy src/` only (per tasks.md 13.1),
  so this is not a CI gate, but the file is clean under `[tool.mypy] strict
  = true` regardless.

## Spec-coverage audit (Task 3.2, tasks.md line 15)

| Spec case | Test | File:line | Verdict |
|---|---|---|---|
| redacts `api_key` patterns | `test_redact_redacts_sensitive_patterns` rows `api_key-equals`, `api-key-hyphen-equals`, `apikey-no-separator`, `api_key-uppercase` | lines 104-107 | covered (4 variants: `_`, `-`, none, uppercase) |
| redacts `token` patterns | rows `token-equals`, `token-colon-space` | lines 109-110 | covered (`=` and `: ` separators) |
| redacts `bearer` tokens | rows `bearer-title-case`, `bearer-lowercase` | lines 112-113 | covered (`Bearer` and `bearer`) |
| redacts `authorization` headers | rows `authorization-colon`, `authorization-equals` | lines 115-116 | covered (`: ` and `=` separators) |
| truncates to the limit | `test_redact_truncates_to_custom_limit` (limit=10), `test_redact_truncates_to_default_limit` (limit=500), `test_redact_does_not_truncate_string_at_or_below_limit` (exactly 500) | lines 193-211 | covered (custom, default, at-boundary) |
| leaves non-sensitive text unchanged | `test_redact_leaves_non_sensitive_text_unchanged` (4 rows) | lines 214-233 | covered |

Every case in the spec is explicitly tested. No case is missing or weakly
asserted. The `{20,}` threshold (not in the spec but in the implementation)
is additionally tested by `test_redact_preserves_values_below_20_char_threshold`
(4 rows), which is beyond-spec positive coverage.

## Regex-match correctness audit (review dimension 2)

Every redaction test input was verified to actually trigger its target
pattern (no false-positive coverage):

- `api_key={_SECRET}`: `api[_-]?key` matches `api_key` (`_` separator),
  `["']?` (pre-sep) matches empty, `[:=]` matches `=`, `["']?` (post-sep)
  matches empty, `[A-Za-z0-9_\-]{20,}` matches the 32-char value. ✓
- `api-key={_SECRET}`: `[_-]?` matches `-`. ✓
- `apikey={_SECRET}`: `[_-]?` is optional, matches empty — `apikey` matches. ✓
- `API_KEY={_SECRET}`: `(?i)` makes it case-insensitive. ✓
- `token={_SECRET}` / `token: {_SECRET}`: pattern 2, `[:=]` matches `=` / `:`
  with `\s*` absorbing the space. ✓
- `Bearer {_SECRET}` / `bearer {_SECRET}`: pattern 3, `bearer\s+` matches
  `Bearer ` / `bearer ` (case-insensitive). ✓
- `Authorization: {_SECRET}` / `authorization={_SECRET}`: pattern 4. ✓
- Quoted values: `api_key="{_SECRET}"` — post-separator `["']?` consumes `"`,
  value matches, closing `"` survives. ✓

The below-threshold tests (`test_redact_preserves_values_below_20_char_threshold`)
use values of 5-10 chars, correctly below `{20,}`, and do NOT match —
verified by the assertion that the text is returned unchanged. No input
looks like it tests redaction but fails to match the regex.

## Assertion-correctness audit (review dimension 3)

- **Redaction tests** (`test_redact_redacts_sensitive_patterns`, line 140):
  `assert _redact(text) == expected`. The `expected` values are hand-computed
  and differ from the input (e.g. `api_key=[REDACTED]` ≠ `api_key=<32
  chars>`). Not tautological. Would fail if `_redact` returned its input
  unchanged, returned `None`, or used a different replacement string. ✓
- **Quoted-value tests** (line 166): `assert _redact(text) == expected`.
  The expected value `'api_key=[REDACTED]"'` proves the opening quote was
  consumed and the closing quote survived. Would fail if the regex captured
  the closing quote or did not consume the opening quote. ✓
- **Below-threshold tests** (line 190): `assert _redact(text) == expected`
  where `expected == text`. In isolation this cannot distinguish a correct
  function from a no-op, but in context (the redaction tests prove the
  function does transform sensitive input) this is a meaningful negative
  test. ✓
- **Truncation tests** (lines 197, 205, 211): `assert _redact("a"*100,
  limit=10) == "a"*10` would fail if truncation were removed or off-by-one.
  `assert len(result) == 500` + `assert result == "x"*500` — the second
  implies the first (redundant, see L4), but both are correct. ✓
- **Non-sensitive tests** (line 233): `assert _redact(text) == text`. Same
  reasoning as below-threshold — meaningful in context. The empty-string
  row is vacuous (see L3). ✓
- **Multi-secret test** (line 241): `assert _redact(text) == expected`
  where `expected` has three `[REDACTED]` replacements. Would fail if any
  of the three patterns were removed. ✓

No always-true or tautological assertions found (empty-string row is
vacuous but not tautological — it does prove no exception is raised).

## Truncation-semantics audit (review dimension 4)

| Boundary | Test | File:line | Verdict |
|---|---|---|---|
| Over limit (custom) | 100 chars, limit=10 → 10 chars | line 197 | covered |
| Over limit (default) | 600 chars, limit=500 → 500 chars | line 205 | covered |
| Exactly at limit | 500 chars, limit=500 → 500 chars (no truncation) | line 211 | covered |
| Below limit | non-sensitive texts (4-44 chars), limit=500 → unchanged | lines 214-233 | covered (transitively) |
| limit=0 | — | — | not tested (out of spec) |
| Negative limit | — | — | not tested (out of spec) |

Off-by-one is covered: exactly-at-500 returns full (proves `[:500]` not
[:499]`), 600 returns 500 (proves `[:500]` not `[:501]`). The `text[:limit]`
slice is standard Python; the boundary tests are sufficient for the spec.

The interaction between redaction and truncation is NOT tested — see M1.

## Mutation-coverage audit (review dimension 9)

| Hypothetical break in `_redact` | Redaction tests | Truncation tests | Non-sensitive | Multi-secret | Caught? |
|---|---|---|---|---|---|
| Remove api_key pattern | **FAIL** (4 rows) | pass | pass | **FAIL** | yes |
| Remove token pattern | **FAIL** (2 rows) | pass | pass | **FAIL** | yes |
| Remove bearer pattern | **FAIL** (2 rows) | pass | pass | **FAIL** | yes |
| Remove authorization pattern | **FAIL** (2 rows) | pass | pass | pass (not in multi-secret) | yes |
| Remove `(?i)` flag | **FAIL** (`API_KEY`, `Bearer`, `Token`) | pass | pass | **FAIL** | yes |
| Change `{20,}` to `{10,}` | pass | pass | pass | pass | **FAIL** (below-threshold tests: `shorttoken`=10 chars now matches) | yes |
| Change `{20,}` to `{40,}` | **FAIL** (`_SECRET`=32 chars no longer matches) | pass | pass | **FAIL** | yes |
| Change `[:=]` to `=` only | **FAIL** (`token: `, `Authorization: `) | pass | pass | pass | yes |
| Change `[REDACTED]` to `[HIDDEN]` | **FAIL** (all redaction tests) | pass | pass | **FAIL** | yes |
| Remove truncation (`return text`) | pass | **FAIL** (3 tests) | pass | pass | yes |
| `return text[:limit-1]` | pass | **FAIL** (3 tests) | pass | pass | yes |
| `return text[:limit+1]` | pass | **FAIL** (3 tests) | pass | pass | yes |
| Redact-then-truncate → truncate-then-redact | pass | pass | pass | pass | **no** (see M1) |
| Remove `for` loop (only first pattern) | **FAIL** (token/bearer/auth rows) | pass | pass | **FAIL** | yes |

The suite catches every spec-relevant mutation except the redaction/truncation
order reversal (M1).

## Test-isolation audit (review dimension 5)

- `_redact` is a pure function. `_REDACT_PATTERNS` is a module-level list of
  compiled regexes, never mutated by the function or the tests.
- `_SECRET` (line 97) is a module-level immutable string constant. No
  mutation, no shared mutable state.
- No environment variables are read by `_redact` or the tests.
- No fixtures, no `monkeypatch`, no order dependence. Each parametrize row
  is independent.
- Clean.

## Idiomatic-pytest audit (review dimension 6)

- `@pytest.mark.parametrize` with explicit `ids` on all parametrized tests
  — test IDs are human-readable (`api_key-equals`, `bearer-title-case`,
  `api_key-short-value`, `greeting`, `empty-string`, etc.).
- Naming follows `test_redact_<behavior>` consistently
  (`test_redact_redacts_sensitive_patterns`,
  `test_redact_truncates_to_custom_limit`, etc.).
- No unnecessary fixtures; parametrize is used where parametrize is right
  (multi-input behavior), standalone tests where the behavior is singular
  (custom-limit truncation, multi-secret composition).
- `from __future__ import annotations` + `-> None` on every test function.
- The below-threshold test (`test_redact_preserves_values_below_20_char_threshold`)
  is good idiomatic negative-case parametrization — one parametrize row per
  pattern, proving the threshold protects all four pattern types.

## Type-hints & style audit (review dimension 7)

- All test functions annotated `-> None`; all parametrize parameters typed
  (`text: str`, `expected: str`, `url: str`, `match: str`).
- `from __future__ import annotations` present (line 22).
- `ruff check`: clean. `mypy`: clean.
- PEP 8 compliant; no long lines, consistent naming.
- The `_SECRET` constant is module-level with a clear comment explaining
  why it is 32 chars of alphanumerics (lines 94-97).

## Scope-discipline audit (review dimension 8)

- The module docstring (lines 13-19) documents both `_validate_base_url`
  (3.1) and `_redact` (3.2) and explicitly states tasks 3.3-3.6 helpers
  "live in tasks 3.3-3.6." No scope creep.
- The import (line 26) is `from mem0_mcp_server.server import _redact,
  _validate_base_url` — only the two helpers under test. No imports of
  `_validate_memory_id`, `_error`, `_int_env`, `_with_default_filters`, or
  any other 3.3-3.6 symbol.
- The 3.1 tests (lines 29-87) are unchanged from the version reviewed in
  `review-3.1.md`. The only modification to the pre-3.2 file is the
  expanded module docstring (lines 1-20) and the added `_redact` import on
  line 26 — both necessary and appropriate for appending 3.2 tests to the
  same file. The 3.1 test functions, parametrize rows, and IDs are
  identical.
- The 3.2 tests (lines 90-249) are cleanly separated by a section comment
  (lines 90-92).

## Verdict

pass-with-findings — The implementation is spec-correct, all 25 `_redact`
tests pass, and mutation coverage is strong for every behavior in isolation.
The one Medium finding (M1: redaction/truncation interaction untested) is a
security-relevant robustness gap, not a spec-coverage gap — the spec lists
the two behaviors separately. All other findings are Low (character-class
gaps, vacuous empty-string assertion, redundant length check, multi-secret
coverage of 3/4 patterns, imprecise docstring). None block merging; M1
should be addressed as a follow-up to harden the redaction test suite
against an order-of-operations regression.
