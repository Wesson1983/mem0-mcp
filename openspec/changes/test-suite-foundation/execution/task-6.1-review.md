# Task 6.1 Review — test_schemas.py (Task 6.1 additions)

Reviewer: code-reviewer
Date: 2026-08-23
File: tests/unit/test_schemas.py (lines 1-493, the 6.1 portion)
Commit: a173889397a716072c263fd601b93258556f8a21

## Summary

PASS. Task 6.1 correctly implements `AddMemoryArgs` and `ToolMessage`
coverage and goes beyond the spec's minimum (the spec asks for "accepts
text or messages; exclude_none omits unset; ToolMessage validates
role/content" — the commit also pins `expiration_date` calendar
semantics, falsy-but-set survival, malformed-messages `loc` precision,
and non-string content rejection). All claims traced against
`schemas.py:1-118` and verified empirically (strptime single-digit
acceptance, int-content rejection). L1 docstring inaccuracy fixed
post-review.

## Findings

### CRITICAL
None.

### HIGH
None.

### MEDIUM
None.

### LOW
- [L1] FIXED. `test_add_memory_args_rejects_messages_with_non_string_content`
  docstring originally stated "Pydantic does not coerce `int` -> `str` in
  strict mode" — misleading because the schema is not in strict mode
  (`ToolMessage` uses plain `BaseModel`, no `ConfigDict(strict=True)`);
  rejection happens in lax/default mode. Reworded to: "Pydantic v2
  rejects `int` for `str`-typed fields in lax mode (the default, since
  `ToolMessage` uses plain `BaseModel` with no
  `model_config = ConfigDict(strict=True)`)." Functional behavior was
  always correct; only the explanation was wrong.

## Verification commands run

- `python -m pytest tests/unit/test_schemas.py -q`:
  `61 passed in 0.70s` (6.1 + 6.2 combined; all green)
- `python -m ruff check tests/unit/test_schemas.py`:
  `All checks passed!`
- `python -m mypy tests/unit/test_schemas.py`:
  `Success: no issues found in 2 source files`
- `python -c "from datetime import datetime;
  datetime.strptime('2026-8-23','%Y-%m-%d')"`: succeeds — confirms
  single-digit-month acceptance claim (line 350, 364).
- `python -c "from mem0_mcp_server.schemas import AddMemoryArgs;
  AddMemoryArgs(messages=[{'role':'user','content':123}])"`: raises
  `string_type` — confirms int-rejection claim (functionally correct,
  docstring wording is the only issue).

## Spec coverage matrix

| Spec requirement (tasks.md 6.1) | Test name | Covered? | Notes |
|---|---|---|---|
| AddMemoryArgs accepts `text` | test_add_memory_args_accepts_text_only | YES | line 144 |
| AddMemoryArgs accepts `messages` | test_add_memory_args_accepts_messages_only | YES | line 161; dict->ToolMessage coercion asserted |
| model_dump(exclude_none=True) omits unset optional fields | test_add_memory_args_exclude_none_omits_all_unset_optional_fields | YES | line 249; all 9 optional fields enumerated |
| ToolMessage validates role/content structure | test_toolmessage_rejects_missing_required_field (parametrized) | YES | line 95; loc[0] asserts missing field name |

### Beyond-spec coverage (intentional, valuable)

| Behavior | Test | Notes |
|---|---|---|
| Schema does NOT enforce one-of text/messages | test_add_memory_args_accepts_text_and_messages_simultaneously | Pins that the disjunction lives in the tool fn (server.py:449-460), not the schema |
| Schema accepts neither (tool fn is sole enforcer) | test_add_memory_args_accepts_no_text_and_no_messages | Dumps to `{}`; tool-level error is task 8.3 scope |
| exclude_none keeps falsy-but-set (infer=False, metadata={}, text="") | test_add_memory_args_exclude_none_keeps_falsy_but_set_values | Guards against exclude_defaults=True regression |
| expiration_date strptime calendar-aware (rejects 2026-02-29) | test_add_memory_args_rejects_non_leap_year_february_29 | Distinguishes strptime from naive regex |
| expiration_date accepts single-digit month/day | test_add_memory_args_accepts_valid_iso_expiration_date[single-digit-month] | Pins strptime's lax padding |
| expiration_date rejects wrong separators/order/missing fields | test_add_memory_args_rejects_invalid_expiration_date (parametrized) | 5 reject cases |
| malformed messages -> precise loc ('messages', 0, 'content') | test_add_memory_args_rejects_messages_missing_content | Exact tuple assertion |
| malformed messages -> precise loc ('messages', 0, 'role') | test_add_memory_args_rejects_messages_missing_role | Symmetric |
| non-string content rejected | test_add_memory_args_rejects_messages_with_non_string_content | Pins str-typed field (docstring wording off, see L1) |

## Detailed analysis

### Correctness vs implementation
Traced `schemas.py`:
- `ToolMessage` (lines 22-24): two required `str` fields via `Field(...)`.
  `test_toolmessage_rejects_missing_required_field` parametrizes both
  omissions, asserts `errors()[0]["loc"][0]`. Correct.
- `AddMemoryArgs` (lines 42-69): `text`/`messages` both `Optional` with
  `None` defaults; no model-level one-of validator. The
  `accepts_text_and_messages_simultaneously` and
  `accepts_no_text_and_no_messages` tests pin this. Correct — matches
  the design's "schema validates types and expiration_date format, tool
  fn enforces the disjunction" split.
- `_validate_iso_date` (lines 11-19): `strptime(value, "%Y-%m-%d")`.
  Verified single-digit month parses; verified 2026-02-29 raises. The
  tests reflect actual strptime behavior, not a hypothetical regex.
- `messages: Optional[list[ToolMessage]]` (line 46): Pydantic coerces
  dicts. `test_add_memory_args_accepts_messages_only` (line 183) asserts
  `all(isinstance(m, ToolMessage) for m in args.messages)`. Correct.
- `field_validator("expiration_date")` (line 69): wired into
  `AddMemoryArgs`. All expiration_date tests exercise this path.

### exclude_none contract
The tool functions rely on `args.model_dump(exclude_none=True)` to keep
the wire payload flat (server.py:448). Three tests pin this:
- `omits_all_unset_optional_fields` (line 249): enumerates all 9
  optional fields by name as negative guards — a regression adding a
  new optional field with a non-None default would leak and fail.
- `includes_only_set_optional_fields` (line 280): realistic 4-field
  payload, exact dict equality.
- `keeps_falsy_but_set_values` (line 317): `infer=False`, `metadata={}`,
  `text=""` all survive. Guards against `exclude_defaults=True` swap
  (which would drop falsy defaults). This is the most valuable of the
  three — it catches a subtle, plausible regression.

### Malformed messages loc precision
`test_add_memory_args_rejects_messages_missing_content` (line 442)
asserts `errors()[0]["loc"] == ("messages", 0, "content")` — exact tuple.
This is what `add_memory` catches and surfaces as
`_error("invalid_messages", ...)` (server.py:431-433). The exact-loc
assertion means a regression changing the field name or list nesting is
caught precisely, not hidden behind a string match.

### Design alignment
The design (test-suite-foundation/design.md) explicitly lists "schema
validation" as a unit-layer goal and "Tests patch module constants, not
environment variables" (Decision 7) — N/A here since schemas need no
env patching. The "Non-Goals" section says "Changing any production
code" is out of scope — confirmed: `schemas.py` is unchanged in all
three commits. The beyond-spec coverage aligns with the design's
"characterize the full server surface" goal and the "forward investment"
risk acknowledgment (lines 417-425): the expiration_date and
falsy-but-set tests pay off when `batch-write-guardrails` mutates error
paths.

### Style consistency
Consistent with the rest of the unit suite: long Google-style docstrings
citing `schemas.py`/`server.py` line numbers, section header comments,
parametrize with descriptive `ids`, `# type: ignore[call-arg]` on
Pydantic constructions with omitted optional fields (justified in the
6.2 section header comment at lines 507-513: pydantic mypy plugin
unavailable, so strict mypy raises false-positive "missing named
argument"). No emojis.

## Verdict

- [x] PASS — no actionable findings
- [ ] PASS WITH FINDINGS — minor findings, fix recommended
- [ ] FAIL — critical/high findings must be fixed before commit

L1 fixed post-review (docstring reworded; ruff/mypy/pytest re-verified
green: 50 passed).
