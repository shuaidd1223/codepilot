"""Codex CLI MCP launch-plan builder."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from codepilot.mcp.launchers import LaunchPlan, MCPServerSpec, normalize_mcp_servers
from codepilot.mcp.launchers.language import with_interaction_instructions


def build_launch_plan(
    *,
    executable: str,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
    session: str | None = None,
    language: str = "en",
) -> LaunchPlan:
    servers = normalize_mcp_servers(mcp_servers)
    config = {"mcp_servers": _codex_servers(servers)}
    config_args = _config_args(servers)
    session_id = str(session or "").strip()
    if prompt:
        # Headless one-shot via `codex exec`.
        command = [
            *_codex_executable_command(executable, interactive=False),
            *config_args,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        command.append(with_interaction_instructions(prompt, language=language))
    else:
        # Interactive TUI. Codex has no equivalent of Claude's --append-system-prompt,
        # so Chinese interaction rules are not injected here; users can set their own
        # preferences in ~/.codex/config.toml if needed.
        if session_id:
            command = [*_codex_executable_command(executable, interactive=True), *config_args, "resume", session_id]
        else:
            command = [*_codex_executable_command(executable, interactive=True), *config_args]
    return LaunchPlan(
        agent="codex",
        command=command,
        env=dict(env or {}),
        mcp_config=config,
        config_args=config_args,
    )


def _codex_executable_command(executable: str, *, interactive: bool) -> list[str]:
    native = _native_windows_codex_exe(executable) if interactive else ""
    if native:
        return [native]
    path = Path(str(executable))
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return [executable]
    codex_js = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if not codex_js.is_file():
        return [executable]
    node = path.parent / "node.exe"
    return [str(node if node.is_file() else "node"), str(codex_js)]


def _native_windows_codex_exe(executable: str) -> str:
    if os.name != "nt":
        return ""
    path = Path(str(executable))
    if path.suffix.lower() == ".exe":
        return ""
    native = shutil.which("codex.exe")
    if not native:
        return ""
    try:
        native_path = Path(native).resolve()
        original = path.resolve()
        if native_path == original:
            return ""
    except Exception:
        pass
    return native


def _codex_servers(servers: list[MCPServerSpec]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.url:
            item: dict[str, Any] = {"url": server.url, "enabled": server.enabled}
            if server.headers:
                item["http_headers"] = dict(server.headers)
        else:
            item = {"command": server.command, "enabled": server.enabled}
            if server.args:
                item["args"] = list(server.args)
            if server.env:
                item["env"] = dict(server.env)
        result[server.name] = item
    return result


def _config_args(servers: list[MCPServerSpec]) -> list[str]:
    args: list[str] = []
    for server in servers:
        prefix = f"mcp_servers.{server.name}"
        values: list[tuple[str, Any]]
        if server.url:
            values = [("url", server.url)]
            if server.headers:
                values.append(("http_headers", dict(server.headers)))
        else:
            values = [("command", server.command)]
            if server.args:
                values.append(("args", list(server.args)))
            if server.env:
                values.append(("env", dict(server.env)))
        values.append(("enabled", server.enabled))
        for key, value in values:
            args.extend(["-c", f"{prefix}.{key}={_toml_literal(value)}"])
    return args


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if "'" not in value and "\n" not in value and "\r" not in value:
            return f"'{value}'"
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        pairs = [
            f"{_toml_key(str(key))}={_toml_literal(item)}"
            for key, item in value.items()
        ]
        return "{" + ",".join(pairs) + "}"
    return json.dumps(str(value), ensure_ascii=False)


def _toml_key(value: str) -> str:
    if value.replace("_", "").replace("-", "").isalnum() and value[:1].isalpha():
        return value
    return json.dumps(value, ensure_ascii=False)
