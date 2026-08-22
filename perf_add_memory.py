"""Performance test for the `add_memory` operation.

Measures two paths end-to-end:
  1. Direct upstream: POST {MEM0_BASE_URL}/memories with X-API-Key
  2. Through the MCP server: tools/call `add_memory` over Streamable HTTP

This isolates upstream LLM fact-extraction + embedding cost from MCP-layer
overhead. Per AGENTS.md, the MCP handshake is driven from Python because
PowerShell mangles inline JSON and `curl` is aliased to Invoke-WebRequest.

Usage:
    python perf_add_memory.py [iterations] [--no-mcp] [--no-direct]

Env: reads MEM0_API_KEY, MEM0_BASE_URL, MEM0_DEFAULT_USER_ID from .env.local
     (or the process environment). MCP endpoint defaults to
     http://localhost:8765/mcp and is overridable via MCP_URL.
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

# Distinct, non-overlapping payloads so each write is a real extraction+embed
# job and not a dedup no-op. Vary length a bit to surface any size sensitivity.
PAYLOADS = [
    "User prefers tab indentation and a 100-column line width for Python files.",
    "The deployment pipeline runs on GitHub Actions and requires two reviewers for the prod stage.",
    "Preferred standup time is 09:30 CET on weekdays; skip on public holidays.",
    "Project uses Postgres 16 with pgvector for embeddings; connection pool size is 20.",
    "User's local model for embeddings is nomic-embed-text served by Ollama on port 11434.",
    "Memory writes are scoped with run_id per pipeline execution and agent_id per factory.",
    "The mem0 OSS server does not expose graph endpoints; do not attempt /graphs calls.",
    "Default HTTP read timeout is 60s; write timeout is 300s to absorb local model latency.",
    "User wants performance baselines captured before any optimization is applied.",
    "Fact extraction prompt should be tuned to extract preferences, facts, and relations only.",
]


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
        f"{st['label']:<28} n={st['n']:<3} "
        f"min={st['min_s']:>7.3f}s  median={st['median_s']:>7.3f}s  "
        f"mean={st['mean_s']:>7.3f}s  p95={st['p95_s']:>7.3f}s  "
        f"max={st['max_s']:>7.3f}s  stdev={st['stdev_s']:>6.3f}s"
    )


# --- Direct upstream -------------------------------------------------------

def bench_direct(n: int) -> list[float]:
    """Time n direct POST /memories calls against the upstream OSS server."""
    print(f"\n[1/2] Direct upstream: POST {BASE_URL}/memories  (n={n})", flush=True)
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    samples: list[float] = []
    for i in range(n):
        run_id = f"perf-{uuid.uuid4().hex[:8]}"
        body = {
            "messages": [{"role": "user", "content": PAYLOADS[i % len(PAYLOADS)]}],
            "user_id": DEFAULT_USER,
            "run_id": run_id,
            "metadata": {"perf_run": "direct", "iter": i},
        }
        t0 = time.perf_counter()
        try:
            r = requests.post(
                f"{BASE_URL}/memories", json=body, headers=headers, timeout=WRITE_TIMEOUT
            )
        except requests.RequestException as exc:
            print(f"  iter {i}: REQUEST FAILED: {exc}", file=sys.stderr)
            continue
        dt = time.perf_counter() - t0
        ok = r.status_code < 400
        flag = "ok " if ok else f"HTTP{r.status_code}"
        print(f"  iter {i:>2}: {dt:7.3f}s  [{flag}]  run_id={run_id}", flush=True)
        if ok:
            samples.append(dt)
    return samples


# --- Through MCP server ----------------------------------------------------

def _mcp_init(session: requests.Session) -> str:
    """Perform the Streamable HTTP initialize handshake; return session id."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    r = session.post(
        MCP_URL,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "perf-add-memory", "version": "0.1.0"},
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    sid = r.headers.get("mcp-session-id")
    if not sid:
        raise RuntimeError(f"no mcp-session-id in response headers: {dict(r.headers)}")
    # notifications/initialized — no response body expected (202). Give the server
    # a moment to register the initialized state before we issue requests; without
    # this, a tools/list fired immediately afterwards has been observed to return
    # an empty tool list.
    session.post(
        MCP_URL,
        headers={**headers, "mcp-session-id": sid},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=30,
    )
    time.sleep(0.2)
    return sid


def _mcp_rpc(session: requests.Session, sid: str, id_: int, method: str, params: dict | None = None) -> dict:
    """Generic JSON-RPC call over the MCP Streamable HTTP transport."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "mcp-session-id": sid,
    }
    payload: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        payload["params"] = params
    r = session.post(MCP_URL, headers=headers, json=payload, timeout=WRITE_TIMEOUT + 30)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        # SSE frames: event: message\ndata: {...}\n\n. A JSON message may span
        # multiple data lines per the SSE spec, so collect and join them.
        data_lines: list[str] = []
        for line in r.text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            return json.loads("\n".join(data_lines))
        raise RuntimeError(f"no data line in SSE response: {r.text[:300]}")
    return r.json()


def _mcp_call_tool(session: requests.Session, sid: str, id_: int, tool: str, args: dict) -> dict:
    """Convenience wrapper for tools/call."""
    return _mcp_rpc(session, sid, id_, "tools/call", {"name": tool, "arguments": args})


def bench_mcp(n: int) -> list[float]:
    print(f"\n[2/2] Via MCP server: tools/call add_memory  (n={n})", flush=True)
    s = requests.Session()
    sid = _mcp_init(s)
    print(f"  mcp-session-id={sid}", flush=True)

    # Sanity: tools/list
    tl = _mcp_rpc(s, sid, 2, "tools/list", {})
    tools = [t["name"] for t in tl.get("result", {}).get("tools", [])]
    print(f"  tools/list -> {len(tools)} tools: {tools}", flush=True)
    if "add_memory" not in tools:
        print(f"  RAW tools/list response: {json.dumps(tl)[:600]}", flush=True)
        raise RuntimeError("add_memory tool not present")

    samples: list[float] = []
    for i in range(n):
        run_id = f"perf-mcp-{uuid.uuid4().hex[:8]}"
        args = {
            "text": PAYLOADS[i % len(PAYLOADS)],
            "user_id": DEFAULT_USER,
            "run_id": run_id,
            "metadata": {"perf_run": "mcp", "iter": i},
        }
        t0 = time.perf_counter()
        try:
            resp = _mcp_call_tool(s, sid, 100 + i, "add_memory", args)
        except Exception as exc:
            print(f"  iter {i}: CALL FAILED: {exc}", file=sys.stderr)
            continue
        dt = time.perf_counter() - t0
        err = resp.get("error")
        result = resp.get("result", {}) if isinstance(resp.get("result"), dict) else {}
        is_error = result.get("isError")
        # The mem0 MCP tool returns upstream errors inside content/structuredContent
        # with isError=false, so inspect the payload for an "error" key too.
        structured = result.get("structuredContent", {}) if isinstance(result, dict) else {}
        inner = structured.get("result", {}) if isinstance(structured, dict) else {}
        inner_err = inner.get("error") if isinstance(inner, dict) else None
        if inner_err is None:
            # Fall back to scanning the text content for an error blob.
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
            flag = f"upstream_err({str(inner_err)[:200]})"
        print(f"  iter {i:>2}: {dt:7.3f}s  [{flag}]  run_id={run_id}", flush=True)
        if not err and not is_error and not inner_err:
            samples.append(dt)
    return samples


# --- Main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iterations", nargs="?", type=int, default=5,
                    help="iterations per path (default 5)")
    ap.add_argument("--no-direct", action="store_true", help="skip direct upstream bench")
    ap.add_argument("--no-mcp", action="store_true", help="skip MCP bench")
    args = ap.parse_args()

    print(f"MEM0_BASE_URL     = {BASE_URL}")
    print(f"MEM0_DEFAULT_USER = {DEFAULT_USER}")
    print(f"MCP_URL           = {MCP_URL}")
    print(f"WRITE_TIMEOUT     = {WRITE_TIMEOUT}s")
    print(f"iterations        = {args.iterations}")

    results: list[dict] = []
    if not args.no_direct:
        d = bench_direct(args.iterations)
        if d:
            results.append(_stats("direct POST /memories", d))
    if not args.no_mcp:
        m = bench_mcp(args.iterations)
        if m:
            results.append(_stats("MCP tools/call add_memory", m))

    print("\n=== Summary ===")
    for st in results:
        print(_fmt(st))

    if len(results) == 2:
        overhead = round(results[1]["median_s"] - results[0]["median_s"], 3)
        pct = round(100 * overhead / results[0]["median_s"], 1) if results[0]["median_s"] else 0
        print(f"\nMCP overhead vs direct (median): {overhead:+.3f}s ({pct:+.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
