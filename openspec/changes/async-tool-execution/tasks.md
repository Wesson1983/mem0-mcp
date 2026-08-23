## 0. Prerequisite and test-marker cleanup

- [ ] 0.1 Confirm `test-suite-foundation` is applied and `pytest -m "not e2e"`
  is green (211 passed, 13 deselected, 1 xfailed) before starting any code
  change in this file.
- [ ] 0.2 Remove the `xfail(strict=True)` marker from
  `tests/integration/test_concurrency.py::test_event_loop_yields_during_write_read_in_parallel`
  (added by `test-suite-foundation` task 9.1). Once the tool functions are
  async and `requests` calls are offloaded via `anyio.to_thread.run_sync`, the
  read completes concurrently with the write and `read_elapsed < 1.0` holds.
  With `strict=True`, leaving the marker in place turns the now-passing test
  into an `XPASS` failure, so removal is mandatory. After removal, verify the
  test passes (not xfails) under this change.

## 1. Convert Mem0OSSClient to async

- [ ] 1.1 Add `import functools` and `import anyio` to `server.py` imports (neither is currently imported; `anyio` is a transitive dependency via `mcp[cli]`, `functools` is stdlib). Verify both imports succeed and are placed per the existing import grouping (stdlib block, then third-party block).
- [ ] 1.2 Convert `Mem0OSSClient._call` to `async def`. Offload the `self._session.request(...)` call via `anyio.to_thread.run_sync`. Because `run_sync` does not forward keyword arguments, bind them first with `functools.partial(self._session.request, method=method, url=self._url(path), params=params, json=json_body, timeout=timeout)` and pass that partial to `await anyio.to_thread.run_sync(...)`. Keep the `try/except requests.RequestException` block around the `await`. Verify the method signature and error handling are unchanged except for `async`/`await`, and that all five arguments still reach `requests.Session.request`.
- [ ] 1.3 Convert all `Mem0OSSClient` wrapper methods to `async def` and `await self._call(...)`: `add`, `search`, `list_memories`, `get`, `update`, `delete`, `delete_all`, `history`, `list_entities`, `delete_entity`. Verify each method is `async def` and uses `await`.

## 2. Convert tool functions to async

- [ ] 2.1 Convert `add_memory` to `async def` and `await` the `_client(...).add(body)` call. Verify the function compiles and returns the same result type.
- [ ] 2.2 Convert `search_memories` to `async def` and `await` the `_client(...).search(body)` call. Verify compilation.
- [ ] 2.3 Convert `get_memories` to `async def` and `await` the `_client(...).list_memories(params)` call. Verify compilation.
- [ ] 2.4 Convert `delete_all_memories` to `async def` and `await` the `_client(...).delete_all(params)` call. Verify compilation.
- [ ] 2.5 Convert `list_entities` to `async def` and `await` the `_client(...).list_entities()` call. Verify compilation.
- [ ] 2.6 Convert `get_memory` to `async def` and `await` the `_client(...).get(memory_id)` call. Verify compilation.
- [ ] 2.7 Convert `get_memory_history` to `async def` and `await` the `_client(...).history(memory_id)` call. Verify compilation.
- [ ] 2.8 Convert `update_memory` to `async def` and `await` the `_client(...).update(memory_id, body)` call. Verify compilation.
- [ ] 2.9 Convert `delete_memory` to `async def` and `await` the `_client(...).delete(memory_id)` call. Verify compilation.
- [ ] 2.10 Convert `delete_entities` to `async def` and `await` the `_client(...).delete_entity(entity_type, entity_id)` call. Verify compilation.

## 3. End-to-end verification

- [ ] 3.1 Rebuild the Docker image and restart the container. Verify the server starts without errors and `tools/list` returns 10 tools.
- [ ] 3.2 Call `add_memory` via MCP with a single text message and verify it succeeds (same behavior as before).
- [ ] 3.3 Call `search_memories` via MCP and verify it succeeds (same behavior as before).
- [ ] 3.4 Call `list_entities` via MCP and verify it succeeds (same behavior as before).
- [ ] 3.5 Verify concurrent read during write: start an `add_memory` call (20s+), and while it is in flight, call `get_memories` from a second MCP session. Verify the read completes without waiting for the write to finish (previously the read would block until the write completed).
