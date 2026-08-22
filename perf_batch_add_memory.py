"""Performance test for batch `add_memory` — N calls, each with M messages.

A single add_memory call with `messages=[...20 turns...]` does ONE LLM
extraction call (one 8.4K-token system-prompt prefill) for all 20 records,
amortizing the dominant cost. This compares batch throughput vs single-record
throughput.

Usage:
    python perf_batch_add_memory.py [calls=5] [batch_size=20] [--no-mcp] [--no-direct]

Env: reads MEM0_API_KEY, MEM0_BASE_URL, MEM0_DEFAULT_USER_ID from .env.local.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(".env.local"))

API_KEY = os.environ["MEM0_API_KEY"]
BASE_URL = os.environ["MEM0_BASE_URL"].rstrip("/")
DEFAULT_USER = os.environ.get("MEM0_DEFAULT_USER_ID", "mem0-mcp")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:8765/mcp")
WRITE_TIMEOUT = int(os.environ.get("MEM0_HTTP_TIMEOUT", "300"))

# 20 distinct messages per batch (10 user + 10 assistant turns) — varied
# content so each is a real extraction+embed job, not a dedup no-op.
BATCH_MESSAGES = []
for _i in range(10):
    BATCH_MESSAGES.append({"role": "user", "content": f"Record {_i}: User prefers setting {_i} for configuration option {_i % 5}."})
    BATCH_MESSAGES.append({"role": "assistant", "content": f"Record {_i}: Acknowledged preference for setting {_i}."})


def _stats(label: str, samples_s: list[float]) -> dict:
    n = len(samples_s)
    s = sorted(samples_s)
    return {
        "label": label,
        "n": n,
        "min_s": round(s[0], 3),
        "median_s": round(statistics.median(s), 3),
        "mean_s": round(statistics.fmean(s), 3),
        "p95_s": round(s[min(len(s) - 1, int(0.95 * (n - 1)))], 3),
        "max_s": round(s[-1], 3),
        "stdev_s": round(statistics.pstdev(s), 3) if n > 1 else 0.0,
    }


def _fmt(st: dict) -> str:
    return (
        f"{st['label']:<32} n={st['n']:<3} "
        f"min={st['min_s']:>7.3f}s  median={st['median_s']:>7.3f}s  "
        f"mean={st['mean_s']:>7.3f}s  p95={st['p95_s']:>7.3f}s  "
        f"max={st['max_s']:>7.3f}s  stdev={st['stdev_s']:>6.3f}s"
    )


# --- Direct upstream -------------------------------------------------------

def bench_direct(n: int, batch: int, delay: float = 5.0) -> list[float]:
    print(f"\n[1/2] Direct upstream: POST {BASE_URL}/memories  ({n} calls x {batch} msgs)", flush=True)
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    samples: list[float] = []
    for i in range(n):
        if i > 0 and delay > 0:
            time.sleep(delay)
        run_id = f"batch-direct-{uuid.uuid4().hex[:8]}"
        body = {
            "messages": BATCH_MESSAGES[:batch],
            "user_id": DEFAULT_USER,
            "run_id": run_id,
            "metadata": {"perf_run": "batch-direct", "iter": i, "batch_size": batch},
        }
        t0 = time.perf_counter()
        try:
            r = requests.post(f"{BASE_URL}/memories", json=body, headers=headers, timeout=WRITE_TIMEOUT)
        except requests.RequestException as exc:
            print(f"  call {i}: REQUEST FAILED: {exc}", file=sys.stderr)
            continue
        dt = time.perf_counter() - t0
        ok = r.status_code < 400
        flag = "ok " if ok else f"HTTP{r.status_code}"
        n_results = 0
        if ok:
            try:
                n_results = len(r.json().get("results", []))
            except Exception:
                pass
        print(f"  call {i}: {dt:7.3f}s  [{flag}]  results={n_results}  run_id={run_id}", flush=True)
        if ok:
            samples.append(dt)
    return samples


# --- Through MCP server ----------------------------------------------------

def _mcp_init(session: requests.Session) -> str:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = session.post(
        MCP_URL,
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "perf-batch", "version": "0.1.0"},
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    sid = r.headers.get("mcp-session-id")
    if not sid:
        raise RuntimeError(f"no mcp-session-id: {dict(r.headers)}")
    session.post(
        MCP_URL,
        headers={**headers, "mcp-session-id": sid},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=30,
    )
    time.sleep(0.2)
    return sid


def _mcp_rpc(session: requests.Session, sid: str, id_: int, method: str, params: dict | None = None) -> dict:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "mcp-session-id": sid}
    payload: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        payload["params"] = params
    r = session.post(MCP_URL, headers=headers, json=payload, timeout=WRITE_TIMEOUT + 30)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        data_lines: list[str] = []
        for line in r.text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            return json.loads("\n".join(data_lines))
        raise RuntimeError(f"no data line in SSE: {r.text[:300]}")
    return r.json()


def bench_mcp(n: int, batch: int, delay: float = 5.0) -> list[float]:
    print(f"\n[2/2] Via MCP: tools/call add_memory  ({n} calls x {batch} msgs)", flush=True)
    s = requests.Session()
    sid = _mcp_init(s)
    tl = _mcp_rpc(s, sid, 2, "tools/list", {})
    tools = [t["name"] for t in tl.get("result", {}).get("tools", [])]
    print(f"  tools/list -> {len(tools)} tools", flush=True)
    if "add_memory" not in tools:
        raise RuntimeError("add_memory tool not present")

    samples: list[float] = []
    for i in range(n):
        if i > 0 and delay > 0:
            time.sleep(delay)
        run_id = f"batch-mcp-{uuid.uuid4().hex[:8]}"
        args = {
            "messages": BATCH_MESSAGES[:batch],
            "user_id": DEFAULT_USER,
            "run_id": run_id,
            "metadata": {"perf_run": "batch-mcp", "iter": i, "batch_size": batch},
        }
        t0 = time.perf_counter()
        try:
            resp = _mcp_rpc(s, sid, 100 + i, "tools/call", {"name": "add_memory", "arguments": args})
        except Exception as exc:
            print(f"  call {i}: CALL FAILED: {exc}", file=sys.stderr)
            continue
        dt = time.perf_counter() - t0
        err = resp.get("error")
        result = resp.get("result", {}) if isinstance(resp.get("result"), dict) else {}
        is_error = result.get("isError")
        structured = result.get("structuredContent", {}) if isinstance(result, dict) else {}
        inner = structured.get("result", {}) if isinstance(structured, dict) else {}
        inner_err = inner.get("error") if isinstance(inner, dict) else None
        if inner_err is None:
            for c in result.get("content", []) or []:
                txt = c.get("text", "") if isinstance(c, dict) else ""
                if '"error"' in txt:
                    inner_err = txt[:120]
                    break
        flag = "ok "
        if err:
            flag = f"rpc_err({err.get('code')})"
        elif is_error:
            flag = "tool_err"
        elif inner_err:
            flag = f"upstream_err({str(inner_err)[:60]})"
        print(f"  call {i}: {dt:7.3f}s  [{flag}]  run_id={run_id}", flush=True)
        if not err and not is_error and not inner_err:
            samples.append(dt)
    return samples


# --- Main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("calls", nargs="?", type=int, default=5, help="number of add_memory calls (default 5)")
    ap.add_argument("batch_size", nargs="?", type=int, default=20, help="messages per call (default 20)")
    ap.add_argument("--no-direct", action="store_true")
    ap.add_argument("--no-mcp", action="store_true")
    ap.add_argument("--delay", type=float, default=5.0,
                    help="seconds to wait between calls (lets LM Studio recover)")
    args = ap.parse_args()

    print(f"MEM0_BASE_URL     = {BASE_URL}")
    print(f"MEM0_DEFAULT_USER = {DEFAULT_USER}")
    print(f"MCP_URL           = {MCP_URL}")
    print(f"WRITE_TIMEOUT     = {WRITE_TIMEOUT}s")
    print(f"calls             = {args.calls}")
    print(f"batch_size        = {args.batch_size} messages/call")
    print(f"total messages    = {args.calls * args.batch_size}")

    results: list[dict] = []
    if not args.no_direct:
        d = bench_direct(args.calls, args.batch_size, args.delay)
        if d:
            results.append(_stats(f"direct POST /memories ({args.batch_size}/call)", d))
    if not args.no_mcp:
        m = bench_mcp(args.calls, args.batch_size, args.delay)
        if m:
            results.append(_stats(f"MCP add_memory ({args.batch_size}/call)", m))

    print("\n=== Summary ===")
    for st in results:
        print(_fmt(st))

    # Throughput: records/sec
    print(f"\n=== Throughput (records = messages stored) ===")
    for st in results:
        total_records = st["n"] * args.batch_size
        total_time = st["mean_s"] * st["n"]
        rec_per_s = total_records / total_time if total_time else 0
        ms_per_rec = (total_time / total_records * 1000) if total_records else 0
        print(f"  {st['label']:<32} {total_records} records in {total_time:.1f}s  "
              f"= {rec_per_s:.2f} rec/s  ({ms_per_rec:.0f} ms/rec)")

    if len(results) == 2:
        overhead = round(results[1]["median_s"] - results[0]["median_s"], 3)
        pct = round(100 * overhead / results[0]["median_s"], 1) if results[0]["median_s"] else 0
        print(f"\nMCP overhead vs direct (median per call): {overhead:+.3f}s ({pct:+.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
