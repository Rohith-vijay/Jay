"""MCP (Model Context Protocol) client: plug outside tool servers into Jay.

Idea from OpenJarvis (Apache-2.0, https://github.com/open-jarvis/OpenJarvis), written small:
only the stdio transport and the three calls Jay needs (initialize, tools/list, tools/call).

Servers are listed in `data/mcp.json`:

    {"servers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}}}}

Each tool a server offers becomes a Jay tool named `mcp_<server>_<tool>`. Because these tools
belong to someone else's code, every call needs the user's approval, and what comes back is
treated as untrusted text. A server that fails to start is skipped, never a reason for Jay not
to start.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .tools.registry import Registry, Tool

PROTOCOL = "2024-11-05"
START_TIMEOUT = 30
CALL_TIMEOUT = 90


class MCPError(Exception):
    pass


class MCPServer:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None):
        self.name = name
        resolved = shutil.which(command) or command            # npx, uvx etc. are .cmd files on Windows
        self.proc = subprocess.Popen(
            [resolved, *(args or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, **{k: str(v) for k, v in (env or {}).items()}}, text=True, encoding="utf-8",
            bufsize=1)
        self._lock = threading.Lock()
        self._next_id = 0

    def _send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            result: list = []

            def read() -> None:
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        result.append(MCPError(f"{self.name} closed"))
                        return
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue                                # logging noise, not protocol
                    if msg.get("id") == rid and ("result" in msg or "error" in msg):
                        result.append(msg)
                        return

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(timeout)
            if not result:
                self.close()
                raise MCPError(f"{self.name} did not answer {method} within {timeout:.0f} seconds")
            if isinstance(result[0], Exception):
                raise result[0]
            if "error" in result[0]:
                raise MCPError(str(result[0]["error"].get("message", result[0]["error"])))
            return result[0]["result"]

    def start(self) -> list[dict]:
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "jay", "version": "1"}}, START_TIMEOUT)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.request("tools/list", {}, START_TIMEOUT).get("tools", [])

    def call(self, tool: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        parts = []
        for item in result.get("content", []):
            parts.append(item.get("text", "") if item.get("type") == "text" else f"[{item.get('type', 'data')} content]")
        text = "\n".join(p for p in parts if p) or "(no output)"
        if result.get("isError"):
            raise MCPError(text)
        return text

    def close(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


def _safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_")[:40]


def register_server(reg: Registry, server: MCPServer, tools: list[dict]) -> list[str]:
    """Add a started server's tools to the registry. Returns the Jay names."""
    added = []
    for spec in tools:
        raw = spec.get("name", "")
        name = f"mcp_{_safe_name(server.name)}_{_safe_name(raw)}"[:64]
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")

        def call(_raw=raw, **kwargs) -> str:
            return server.call(_raw, kwargs)

        reg._tools[name] = Tool(
            name, f"[{server.name}] {spec.get('description', raw)}"[:1000], schema, call,
            confirm=lambda a, _n=name: f"Use the external tool {_n} with {json.dumps(a)[:300]}")
        added.append(name)
    return added


def load_servers(reg: Registry, data_dir: Path, report=print) -> list[MCPServer]:
    """Start every server in data/mcp.json on a background thread, so Jay's start-up never waits."""
    path = data_dir / "mcp.json"
    if not path.exists():
        return []
    try:
        config = json.loads(path.read_text(encoding="utf-8")).get("servers", {})
    except Exception as exc:
        report(f"mcp.json could not be read: {exc}")
        return []
    servers: list[MCPServer] = []

    def start(name: str, spec: dict) -> None:
        try:
            server = MCPServer(name, spec["command"], spec.get("args"), spec.get("env"))
        except Exception as exc:
            report(f"MCP server '{name}' did not start: {exc}")
            return
        try:
            tools = server.start()
        except Exception as exc:
            server.close()
            report(f"MCP server '{name}' failed: {exc}")
            return
        servers.append(server)
        register_server(reg, server, tools)

    for name, spec in config.items():
        threading.Thread(target=start, args=(name, spec), daemon=True).start()
    return servers
