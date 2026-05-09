"""Shared pytest bootstrap for the CodePilot test suite.

Normalises stdio to UTF-8 on Windows so assertion diffs containing Chinese
(e.g. reviewer verdict strings, task titles) render as readable text
instead of GBK mojibake (``û�����``). Without this, Python defaults to
the ``cp936`` code page when pytest writes failure reports, mangling
every UTF-8 string that flows through stderr.

Kept intentionally tiny: the CLI entrypoint already calls
:func:`codepilot.console_encoding.configure_console_encoding` on startup,
but pytest bypasses that path, so we re-use the helper here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from codepilot.core.console_encoding import configure_console_encoding
from codepilot.storage import database as db


configure_console_encoding()
os.environ.setdefault("CODEPILOT_DESKTOP_NOTIFY", "0")


@pytest.fixture
def mcp_stdio_smoke_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the CLI MCP server over stdio with an isolated project fixture."""

    db_path = tmp_path / "tasks.db"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-AGENTS.toml"))
    db.init_db()
    db.register_project("demo", str(project_path))

    shim_root = tmp_path / "mcp_sdk_shim"
    _write_fastmcp_stdio_shim(shim_root)

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path_entries = [str(shim_root), str(repo_root)]
    if env.get("PYTHONPATH"):
        python_path_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_entries)
    env["CODEPILOT_DB_PATH"] = str(db_path)
    env["CODEPILOT_GLOBAL_CONFIG_PATH"] = str(tmp_path / "missing-AGENTS.toml")
    env["CODEPILOT_DESKTOP_NOTIFY"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codepilot",
            "mcp",
            "serve",
            "--transport",
            "stdio",
            "--project",
            "demo",
        ],
        cwd=repo_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    client = _MCPStdioSmokeClient(process, project_path)
    try:
        client.initialize()
        yield client
    finally:
        client.close()


class _MCPStdioSmokeClient:
    def __init__(self, process: subprocess.Popen[str], project_path: Path) -> None:
        self.process = process
        self.project_path = project_path
        self._next_id = 1

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        assert result["serverInfo"]["name"] == "CodePilot"
        self.notify("notifications/initialized")

    def list_tools(self) -> list[dict[str, Any]]:
        return self.request("tools/list")["tools"]

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self._read_response()
        assert response["id"] == request_id
        if "error" in response:
            raise AssertionError(response["error"])
        return response["result"]

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.returncode not in (0, None):
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise AssertionError(f"MCP server exited with {self.process.returncode}: {stderr}")

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise AssertionError("MCP server stdin is unavailable")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read_response(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise AssertionError("MCP server stdout is unavailable")
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise AssertionError(f"MCP server stopped before responding: {stderr}")
        return json.loads(line)


def _write_fastmcp_stdio_shim(root: Path) -> None:
    package_dir = root / "mcp" / "server"
    package_dir.mkdir(parents=True)
    (root / "mcp" / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "fastmcp.py").write_text(
        dedent(
            r'''
            from __future__ import annotations

            import asyncio
            import json
            import sys


            class FastMCP:
                def __init__(self, name: str) -> None:
                    self.name = name
                    self._tools = {}

                def tool(self, *, name: str, description: str = ""):
                    def decorator(func):
                        self._tools[name] = {"func": func, "description": description}
                        return func

                    return decorator

                def run(self, *args, **kwargs) -> None:
                    while True:
                        line = sys.stdin.readline()
                        if not line:
                            return
                        message = json.loads(line)
                        if "id" not in message:
                            continue
                        response = self._handle_request(message)
                        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                        sys.stdout.flush()

                def _handle_request(self, message):
                    method = message.get("method")
                    params = message.get("params") or {}
                    try:
                        if method == "initialize":
                            result = {
                                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": self.name, "version": "0"},
                            }
                        elif method == "ping":
                            result = {}
                        elif method == "tools/list":
                            result = {
                                "tools": [
                                    {
                                        "name": name,
                                        "description": tool["description"],
                                        "inputSchema": {"type": "object"},
                                    }
                                    for name, tool in self._tools.items()
                                ]
                            }
                        elif method == "tools/call":
                            result = asyncio.run(
                                self._call_tool(params.get("name"), params.get("arguments") or {})
                            )
                        else:
                            raise ValueError(f"unsupported method: {method}")
                        return {"jsonrpc": "2.0", "id": message["id"], "result": result}
                    except Exception as exc:
                        return {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32000, "message": str(exc)},
                        }

                async def _call_tool(self, name, arguments):
                    tool = self._tools[name]
                    result = await tool["func"](**arguments)
                    if isinstance(result, dict) and {"isError", "content", "structuredContent"} <= set(result):
                        return result
                    return {
                        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "structuredContent": result,
                    }
            '''
        ).lstrip(),
        encoding="utf-8",
    )

