"""Codex CLI MCP launch-plan builder."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from codepilot.mcp.launchers import LaunchPlan, MCPServerSpec, normalize_mcp_servers


def build_launch_plan(
    *,
    executable: str,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
) -> LaunchPlan:
    servers = normalize_mcp_servers(mcp_servers)
    config = {"mcp_servers": _codex_servers(servers)}
    config_args = _config_args(servers)
    command = [
        executable,
        *config_args,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if prompt:
        command.append(prompt)
    return LaunchPlan(
        agent="codex",
        command=command,
        env=dict(env or {}),
        mcp_config=config,
        config_args=config_args,
    )


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
