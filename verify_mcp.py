"""Verify the mem0-mcp-oss container end-to-end via the MCP Streamable HTTP transport.

Steps (per AGENTS.md):
1. POST /mcp `initialize` -> capture mcp-session-id
2. POST /mcp `notifications/initialized` with that header
3. POST /mcp `tools/list` -> expect 10 tools
4. POST /mcp `tools/call` `list_entities` -> proves auth works (non-401)
"""

import json
import sys

import requests

URL = "http://localhost:8765/mcp"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
_id = 0


def next_id():
    global _id
    _id += 1
    return _id


def post(session, headers, payload):
    r = session.post(URL, headers=headers, json=payload, timeout=60)
    return r


def parse_sse(text):
    """Pull the first JSON-RPC `result`/`error` out of an SSE stream."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if "result" in obj or "error" in obj:
            return obj
    return None


def main():
    s = requests.Session()

    # 1. initialize
    init = {
        "jsonrpc": "2.0",
        "id": next_id(),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "verify-script", "version": "0.1"},
        },
    }
    r = post(s, HEADERS, init)
    print(f"[1] initialize -> HTTP {r.status_code}")
    session_id = r.headers.get("mcp-session-id")
    if not session_id:
        print("    FAIL: no mcp-session-id header")
        print("    body:", r.text[:500])
        sys.exit(1)
    print(f"    session_id={session_id}")
    body = parse_sse(r.text) or {}
    if "error" in body:
        print("    FAIL initialize error:", body["error"])
        sys.exit(1)
    server_info = body.get("result", {}).get("serverInfo", {})
    print(f"    server={server_info.get('name')} {server_info.get('version')}")

    # session headers for subsequent requests
    h = dict(HEADERS)
    h["MCP-Session-Id"] = session_id

    # 2. notifications/initialized (no response expected)
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    r = post(s, h, notif)
    print(f"[2] notifications/initialized -> HTTP {r.status_code}")

    # 3. tools/list
    tl = {"jsonrpc": "2.0", "id": next_id(), "method": "tools/list"}
    r = post(s, h, tl)
    print(f"[3] tools/list -> HTTP {r.status_code}")
    body = parse_sse(r.text) or {}
    if "error" in body:
        print("    FAIL tools/list error:", body["error"])
        sys.exit(1)
    tools = body.get("result", {}).get("tools", [])
    names = [t.get("name") for t in tools]
    print(f"    {len(tools)} tools: {names}")
    if len(tools) != 10:
        print(f"    WARN: expected 10 tools, got {len(tools)}")

    # 4. tools/call list_entities
    call = {
        "jsonrpc": "2.0",
        "id": next_id(),
        "method": "tools/call",
        "params": {"name": "list_entities", "arguments": {}},
    }
    r = post(s, h, call)
    print(f"[4] tools/call list_entities -> HTTP {r.status_code}")
    body = parse_sse(r.text) or {}
    if "error" in body:
        print("    FAIL list_entities error:", body["error"])
        sys.exit(1)
    result = body.get("result", {})
    content = result.get("content", [])
    if content:
        first = content[0]
        text = first.get("text", "")
        # Auth-failure would surface as http_401 in the text payload.
        if "http_401" in text:
            print("    FAIL: auth failed (http_401 in payload)")
            print("    payload:", text[:500])
            sys.exit(1)
        is_error = result.get("isError", False)
        print(f"    isError={is_error}")
        print(f"    payload[:300]={text[:300]}")
    else:
        print("    empty content")
        sys.exit(1)

    # 5. add_memory round-trip (exercises LLM fact extraction + embeddings)
    add = {
        "jsonrpc": "2.0",
        "id": next_id(),
        "method": "tools/call",
        "params": {
            "name": "add_memory",
            "arguments": {"text": "The user prefers concise answers and dislikes emoji."},
        },
    }
    r = post(s, h, add)
    print(f"[5] tools/call add_memory -> HTTP {r.status_code}")
    body = parse_sse(r.text) or {}
    if body.get("result", {}).get("isError"):
        print("    FAIL add_memory:", body["result"]["content"][0]["text"][:500])
        sys.exit(1)
    add_text = body["result"]["content"][0]["text"]
    print(f"    add payload[:300]={add_text[:300]}")

    # 6. search_memories for what we just wrote
    search = {
        "jsonrpc": "2.0",
        "id": next_id(),
        "method": "tools/call",
        "params": {
            "name": "search_memories",
            "arguments": {"query": "what does the user prefer?", "top_k": 5},
        },
    }
    r = post(s, h, search)
    print(f"[6] tools/call search_memories -> HTTP {r.status_code}")
    body = parse_sse(r.text) or {}
    if body.get("result", {}).get("isError"):
        print("    FAIL search_memories:", body["result"]["content"][0]["text"][:500])
        sys.exit(1)
    search_text = body["result"]["content"][0]["text"]
    print(f"    search payload[:400]={search_text[:400]}")

    # 7. get_memories for default user
    getm = {
        "jsonrpc": "2.0",
        "id": next_id(),
        "method": "tools/call",
        "params": {"name": "get_memories", "arguments": {}},
    }
    r = post(s, h, getm)
    print(f"[7] tools/call get_memories -> HTTP {r.status_code}")
    body = parse_sse(r.text) or {}
    if body.get("result", {}).get("isError"):
        print("    FAIL get_memories:", body["result"]["content"][0]["text"][:500])
        sys.exit(1)
    print(f"    get payload[:300]={body['result']['content'][0]['text'][:300]}")

    print("\nVERIFICATION PASSED")


if __name__ == "__main__":
    main()
