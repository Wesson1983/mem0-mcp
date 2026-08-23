## Why

The `async-tool-execution` refactor (sync→async tools + `anyio.to_thread.run_sync`
offload) is a behavioral refactor against zero automated tests. The repo declares
`pytest>=8.3.4` in dev deps but has no test files, no `conftest.py`, no CI of any
kind (no `.github/`, no `.gitlab-ci.yml`). The only verification is
`verify_mcp.py` — a manual 7-step JSON-RPC handshake covering 4 of 10 tools,
with no assertions and no concurrency check. The refactor's actual thesis (the
event loop yields during HTTP I/O) is untestable with mocks, because a mocked
`requests.Session.request` returns instantly — there's nothing to yield for.

Without a test suite written against the current sync code first, the refactor's
claim of "no externally observable behavior change" is unverifiable, and
concurrency bugs (dropped `await`, misbound `functools.partial` kwargs,
`requests.Session` thread-safety under real concurrent calls) have no detection
mechanism. This change must land before `async-tool-execution` so the refactor's
job is literally "make these tests still pass."

## What Changes

- Add a three-layer pytest suite under `tests/`:
  - **Unit**: pure helpers (`_validate_base_url`, `_redact`,
    `_validate_memory_id`, `_error`, `_int_env`, `_with_default_filters`),
    `_resolve_settings` env precedence + conflict warnings, `_client` cache
    dedup/eviction/clear, Pydantic schema validation + `exclude_none` payload
    shape.
  - **Integration**: `Mem0OSSClient._call` and all 10 wrapper methods against
    a real-socket fake HTTP server (Starlette + uvicorn, zero new deps — both
    transitive via `mcp[cli]`) with controllable per-route latency. Covers
    happy path, HTTP 4xx/5xx error mapping, `requests.RequestException` →
    cache clear, write-vs-read timeout selection, and `functools.partial`
    kwarg survival. All 10 tool functions tested for happy path + primary
    error paths. Includes two concurrency tests: (1) event loop yields during
    a slow write so a parallel read completes well under the write's latency —
    shipped as `xfail(strict=True)` because it is RED on sync code by design,
    with the marker removed by `async-tool-execution`, (2) N concurrent calls
    through the same cached `Mem0OSSClient` each receive their own response
    (no cross-talk) without exceptions.
  - **E2e**: full MCP transport round-trip (init → tools/list → tools/call)
    against the real Docker container + mem0 OSS + LM Studio, gated on
    `MEM0_E2E=1`. Covers all 10 tools via `tools/call`, auth (bad key →
    `http_401`), and the real-latency concurrency proof (read completes
    during a 20-40s write). Uses dedicated test scopes
    (`user_id="test_e2e_<uuid>"`) with forgiving cleanup in `finally`.
- Add `pytest-asyncio` to dev dependencies with `asyncio_mode = "auto"`.
- Add `.github/workflows/ci.yml` running `ruff check && mypy && pytest -m "not
  e2e"` on push and pull request. E2e excluded by marker — CI does not need
  LM Studio or a running container.
- Add `tests/conftest.py` with the fake-server fixture (Starlette app on
  `127.0.0.1:0`, background uvicorn thread with a bounded startup wait,
  per-route latency config, mutable response dict for per-test status
  injection, and a request-echo recorder so tests can assert what reached
  the wire).
- Add `tests/e2e/conftest.py` with e2e-specific fixtures (skip if
  `MEM0_E2E != "1"`, package-level `e2e` marker, MCP session handshake
  fixture reusing `verify_mcp.py`'s SSE parsing, test-scope fixture with
  best-effort cleanup).

## Capabilities

### New Capabilities
<!-- None. This is a tooling/infrastructure change — tests and CI. No
     spec-level behavior changes. The tests encode existing behavior; they
     do not introduce new behavior. -->

### Modified Capabilities
<!-- None. -->

## Impact

- **Code**: no production code changes. New files: `tests/` directory tree,
  `.github/workflows/ci.yml`, `tests/conftest.py`, `tests/e2e/conftest.py`.
  Tests patch module-level constants (`ENV_BASE_URL`, `ENV_API_KEY`, …)
  rather than environment variables, because `server.py` resolves them at
  import time.
- **Dependencies**: `pytest-asyncio` added to `[dependency-groups].dev` in
  `pyproject.toml`. No new production dependencies. The fake server uses
  `starlette` and `uvicorn`, both already transitive via `mcp[cli]`.
- **Configuration**: `pyproject.toml` gains `[tool.pytest.ini_options]` with
  `asyncio_mode = "auto"`, `testpaths = ["tests"]`, and an `e2e` marker.
- **Behavior**: no change to the MCP server. The test suite characterizes
  existing behavior; it does not modify it.
- **CI**: introduces the repo's first CI workflow. Runs on push and pull
  request. Lint + typecheck + unit/integration tests. E2e gated off.
- **Prerequisite for**: `async-tool-execution` — the refactor must not land
  until this suite is green against the current sync code. Also a prerequisite
  for `batch-write-guardrails` — that change is more behavior-changing (adds
  lock, cooldown, retry, batch limit) and also has no planned verification.
  The suite grows at that point to cover the new error paths it introduces.
