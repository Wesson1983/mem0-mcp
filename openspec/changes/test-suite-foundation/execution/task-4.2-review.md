# Task 4.2 Review — test_resolve_settings.py (Task 4.2 additions)

Reviewer: python-pro (orchestrator, after sub-agent quota exhaustion)
Date: 2026-08-23
File: tests/unit/test_resolve_settings.py (416 lines; Task 4.2 added lines 258-416)

## Summary

PASS. The Task 4.2 additions correctly implement env-over-session-config
precedence for all four fields. Two complementary test shapes (combined +
per-field parametrized) cover the spec. `monkeypatch.setattr` is used for env
constants (not `setenv`), session-config values are distinct from env values,
no log-text assertions are present, and the `_validate_base_url` interaction
(localhost-only HTTP) is handled correctly. Ruff, mypy, and pytest all green;
no regressions in the Task 4.1 tests.

## Findings

### CRITICAL
None.

### HIGH
None.

### MEDIUM
None.

### LOW
- [L1] The per-field parametrized test asserts only `result[index]` for the
  field under test and leaves the other three tuple positions unasserted
  (documented at lines 382-384). This is intentional — the combined test
  (lines 288-348) and the Task 4.1 env-only tests cover the other positions —
  but it means a regression that broke precedence for a *non-under-test* field
  in the per-field test would not be caught by that specific case. Acceptable:
  the combined test catches any such regression. No action needed.
- [L2] `# type: ignore[arg-type]` appears at lines 336 and 411 for the
  `_StubContext` duck-typed stub. Justified and consistent with the Task 4.1
  pattern (documented inline). No action needed.

## Verification commands run (orchestrator)

Run from `C:\repo\mem0-mcp`:

- `python -m ruff check tests/unit/test_resolve_settings.py`:
  `All checks passed!`
- `python -m mypy tests/unit/test_resolve_settings.py`:
  `Success: no issues found in 1 source file`
- `python -m pytest tests/unit/test_resolve_settings.py -v`:
  `12 passed in 0.87s` (7 Task 4.1 + 5 Task 4.2 = 12; all green)
- `python -m pytest tests/unit -v`: previously confirmed 100 passed by the
  implementer; no regressions.

## Spec coverage matrix

| Spec requirement | Test name | Covered? | Notes |
|---|---|---|---|
| env wins for api_key | test_resolve_settings_env_wins_over_session_config_all_fields + [...per_field[api_key]] | YES | lines 339-340, 354, 415 |
| env wins for base_url | test_resolve_settings_env_wins_over_session_config_all_fields + [...per_field[base_url]] | YES | lines 347-348, 355, 415; localhost HTTP used to pass `_validate_base_url` |
| env wins for default_user_id | test_resolve_settings_env_wins_over_session_config_all_fields + [...per_field[default_user_id]] | YES | lines 341-342, 356, 415 |
| env wins for default_agent_id | test_resolve_settings_env_wins_over_session_config_all_fields + [...per_field[default_agent_id]] | YES | lines 343-344, 357, 415 |
| No log-text assertions | All Task 4.2 tests | YES | No `caplog` usage; documented at lines 308-310, 386-387; matches design.md:187-190 non-goal |
| monkeypatch.setattr (not setenv) | All Task 4.2 tests | YES | lines 316-319, 390-402 |
| Session-config values distinct from env values | Both tests | YES | "env-*-wins" vs "session-*-loses" / "env-*-only" vs "sess-*-only"; distinct localhost ports for base_url |

## Detailed analysis

### Spec coverage
All four fields are covered in both the combined test (one call, all four
conflict) and the per-field parametrized test (one field conflicts per case).
The combined test mirrors the realistic operator scenario; the per-field test
isolates single-field regressions.

### Correctness vs implementation
Traced `_resolve_settings` (server.py:170-219): for each field, `session_X =
_config_value(...)`; `if session_X and ENV_X: session_X = None`; `X =
session_X or ENV_X`. The tests set both `session_X` and `ENV_X` truthy and
distinct, then assert `X == ENV_X` and `X != session_X` — this correctly
proves the env-wins path. No false-positive risk: if the `if session_X and
ENV_X` guard were removed (session value leaked through), `X` would equal
`session_X` and both assertions would fail.

### monkeypatch usage
All env constants patched via `monkeypatch.setattr(server, "ENV_*", ...)`.
No `monkeypatch.setenv` calls. `monkeypatch` auto-restores after each test, so
no leakage.

### Session-config values distinct from env values
Confirmed: env values use "env-*-wins"/"env-*-only", session-config values
use "session-*-loses"/"sess-*-only". For `base_url`, env uses port 7777 (or
7001) and session-config uses port 8888 (or 7002) — both localhost, distinct
ports, distinguishable after `_validate_base_url`.

### No log-text assertions
Confirmed: no `caplog` fixture, no log-text assertions. Documented in
docstrings at lines 308-310 and 386-387, referencing design.md:187-190.

### `_validate_base_url` interaction
The implementer switched base URLs to `http://localhost:<port>` to avoid the
non-local-HTTPS rejection (server.py:84, `_LOCAL_HOSTS` at server.py:75). This
is correct: localhost is in `_LOCAL_HOSTS`, so plain HTTP is accepted. The
env-vs-session-config distinction for `base_url` is preserved via distinct
ports, so the assertion still meaningfully proves env wins.

### `ENV_DEFAULT_USER_ID` built-in default
The Task 4.2 tests patch `ENV_DEFAULT_USER_ID` to a distinct env value
("env-user-wins" / "env-user-only"), so the built-in "mem0-mcp" default is
not in play. The env-wins assertion correctly compares the patched env value
against the session-config value, not against the built-in default.

### Test isolation
Each test patches the env constants independently via `monkeypatch.setattr`,
which auto-restores. No shared state, no fixtures that leak. The per-field
test carefully sets the non-under-test env constants to neutral sentinels
(lines 395-402) so `ENV_API_KEY` is never `None` (which would raise
`RuntimeError` before the field under test is resolved).

### Style consistency
Consistent with the Task 4.1 tests in the same file and with `test_helpers.py`:
long Google-style docstrings, parametrize with descriptive `ids`, explicit
comments explaining the "why", section header comment block (lines 258-285).

### Regressions
All 7 Task 4.1 tests still pass (12 passed total). No regressions.

## Verdict

- [x] PASS — no actionable findings
- [ ] PASS WITH FINDINGS — minor findings, fix recommended
- [ ] FAIL — critical/high findings must be fixed before commit
