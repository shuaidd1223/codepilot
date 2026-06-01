# CodePilot
# Author: 帅呆呆 <2264505396@qq.com>
# Repository: https://gitee.com/shuai_dd/workflow
# License: MIT
"""MCP launch-plan builders for chat agent CLIs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codepilot.ai_support.cli_families import get_family


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class LaunchPlan:
    agent: str
    command: list[str]
    env: dict[str, str]
    mcp_config: dict[str, Any]
    config_args: list[str] = field(default_factory=list)
    config_files: dict[str, str] = field(default_factory=dict)


class UnsupportedAgentError(ValueError):
    """Raised when no MCP launcher exists for an agent family."""


def build_mcp_launch_plan(
    agent: str,
    *,
    executable: str | None = None,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
    opencode_config: Any | None = None,
    session: str | None = None,
    opencode_session: str | None = None,  # deprecated alias
    language: str = "en",
    scope: str | None = None,
) -> LaunchPlan:
    """Build a dry launch plan for one supported chat agent family."""
    family = get_family(agent)
    if family is None or family.name not in {"claude", "codex", "opencode"}:
        supported = "claude, codex, opencode"
        raise UnsupportedAgentError(
            f"Unsupported MCP launcher agent: {agent!r}. Supported agents: {supported}."
        )

    effective_session = session if session is not None else opencode_session

    command = executable or family.name
    if family.name == "claude":
        from codepilot.mcp.launchers.claude import build_launch_plan

        return build_launch_plan(
            executable=command,
            prompt=prompt,
            mcp_servers=mcp_servers,
            env=env,
            session=effective_session,
            language=language,
        )
    if family.name == "codex":
        from codepilot.mcp.launchers.codex import build_launch_plan

        return build_launch_plan(
            executable=command,
            prompt=prompt,
            mcp_servers=mcp_servers,
            env=env,
            session=effective_session,
            language=language,
        )

    from codepilot.mcp.launchers.opencode import build_launch_plan

    return build_launch_plan(
        executable=command,
        prompt=prompt,
        mcp_servers=mcp_servers,
        env=env,
        config_path=config_path,
        opencode_config=opencode_config,
        session=effective_session,
        language=language,
        scope=scope,
    )


def normalize_mcp_servers(
    value: Mapping[str, Any] | Iterable[MCPServerSpec] | None,
) -> list[MCPServerSpec]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [_server_from_mapping(str(name), raw) for name, raw in value.items()]
    return [_coerce_server(item) for item in value]


def json_config_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _coerce_server(value: MCPServerSpec) -> MCPServerSpec:
    if isinstance(value, MCPServerSpec):
        return value
    raise TypeError(f"unsupported MCP server spec: {type(value).__name__}")


def _server_from_mapping(name: str, raw: Any) -> MCPServerSpec:
    if not isinstance(raw, Mapping):
        raise TypeError(f"MCP server {name!r} must be a mapping")

    command_raw = raw.get("command", "")
    args_raw = raw.get("args", ())
    if isinstance(command_raw, (list, tuple)):
        parts = [str(item) for item in command_raw]
        command = parts[0] if parts else ""
        args = tuple(parts[1:])
    else:
        command = str(command_raw or "")
        args = tuple(str(item) for item in (args_raw or ()))

    url = str(raw.get("url") or "")
    if not command and not url:
        raise ValueError(f"MCP server {name!r} requires either command or url")

    env = _string_map(raw.get("env") or raw.get("environment") or {})
    headers = _string_map(raw.get("headers") or raw.get("http_headers") or {})
    return MCPServerSpec(
        name=name,
        command=command,
        args=args,
        env=env,
        url=url,
        headers=headers,
        enabled=bool(raw.get("enabled", True)),
    )


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}
