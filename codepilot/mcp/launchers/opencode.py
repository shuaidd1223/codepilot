"""OpenCode MCP launch-plan builder."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from codepilot.mcp.launchers import (
    LaunchPlan,
    MCPServerSpec,
)
from codepilot.opencode.config import OpenCodeConfig
from codepilot.opencode.paths import opencode_runtime_config_path
from codepilot.opencode.profile import build_opencode_profile


def build_launch_plan(
    *,
    executable: str,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
    opencode_config: OpenCodeConfig | None = None,
    session: str | None = None,
) -> LaunchPlan:
    target_path = Path(config_path or opencode_runtime_config_path())
    profile = build_opencode_profile(
        opencode_config,
        mcp_servers=mcp_servers,
        base_path=target_path.parent,
        config_path=target_path,
    )
    command = [executable]
    default_agent = str(profile.config.get("default_agent") or "").strip()
    session_id = str(session or "").strip()
    if prompt:
        command.append("run")
        if default_agent:
            command.extend(["--agent", default_agent])
        if session_id:
            command.extend(["--session", session_id])
        command.append(prompt)
    else:
        if default_agent:
            command.extend(["--agent", default_agent])
        if session_id:
            command.extend(["-s", session_id])
    merged_env = dict(env or {})
    merged_env.update(profile.env)
    return LaunchPlan(
        agent="opencode",
        command=command,
        env=merged_env,
        mcp_config=profile.config,
        config_files=profile.files,
    )
