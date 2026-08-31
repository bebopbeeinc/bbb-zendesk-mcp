#!/usr/bin/env python3
"""End-to-end smoke test: boot an MCP server over stdio and list its tools.

Speaks raw JSON-RPC with nothing but the standard library, deliberately. The
canary exists to catch MCP SDK breakage, so it must not import the SDK itself
or share the failure mode it is trying to detect.

Usage:
    smoke_test.py <command> [args...]      e.g. smoke_test.py uvx zendesk-mcp
"""
import json
import subprocess
import sys
import threading

PROTOCOL_VERSION = "2025-06-18"
EXPECTED_TOOL = "zendesk_get_ticket"
MIN_TOOLS = 20
TIMEOUT_SECONDS = 120


def _rpc(stream, payload):
    stream.write(json.dumps(payload) + "\n")
    stream.flush()


def _read_response(proc, want_id):
    """Read line-delimited JSON until the response with want_id arrives."""
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout before responding")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # Servers may log non-JSON to stdout; ignore and keep reading.
            continue
        if msg.get("id") == want_id:
            return msg


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    command = argv[1:]
    print(f"[smoke] launching: {' '.join(command)}", flush=True)

    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_lines = []
    threading.Thread(
        target=lambda: stderr_lines.extend(iter(proc.stderr.readline, "")),
        daemon=True,
    ).start()

    def fail(message):
        proc.kill()
        proc.wait()
        print(f"[smoke] FAIL: {message}", file=sys.stderr)
        if stderr_lines:
            print("[smoke] server stderr:", file=sys.stderr)
            for line in stderr_lines[-40:]:
                print("  " + line.rstrip(), file=sys.stderr)
        return 1

    result = {}

    def handshake():
        try:
            _rpc(proc.stdin, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "1.0"},
                },
            })
            init = _read_response(proc, 1)
            if "error" in init:
                result["error"] = f"initialize returned an error: {init['error']}"
                return
            server_info = init.get("result", {}).get("serverInfo", {})
            print(f"[smoke] initialized: {server_info}", flush=True)

            _rpc(proc.stdin, {
                "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
            })
            _rpc(proc.stdin, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = _read_response(proc, 2)
            if "error" in listed:
                result["error"] = f"tools/list returned an error: {listed['error']}"
                return
            result["tools"] = [t["name"] for t in listed.get("result", {}).get("tools", [])]
        except Exception as exc:  # surfaced by the caller with server stderr attached
            result["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=handshake, daemon=True)
    worker.start()
    worker.join(TIMEOUT_SECONDS)

    if worker.is_alive():
        return fail(f"server did not complete the handshake within {TIMEOUT_SECONDS}s")
    if "error" in result:
        return fail(result["error"])

    tools = result.get("tools", [])
    print(f"[smoke] server advertised {len(tools)} tools", flush=True)

    proc.stdin.close()
    proc.kill()
    proc.wait()

    if EXPECTED_TOOL not in tools:
        return fail(f"expected tool {EXPECTED_TOOL!r} missing; got {sorted(tools)}")
    if len(tools) < MIN_TOOLS:
        return fail(f"expected at least {MIN_TOOLS} tools, got {len(tools)}")

    print("[smoke] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
