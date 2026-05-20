"""Tool-level OpenCode profile defaults for CodePilot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpenCodeProfileConfig:
    """CodePilot-managed OpenCode TUI/runtime profile defaults."""

    brand_name: str = "CodePilot"
    default_agent: str = "codepilot"
    theme: str = "system"
    scroll_speed: int = 3
    diff_style: str = "auto"
    mouse: bool = True


@dataclass
class OpenCodeAgentConfig:
    """Primary OpenCode agent metadata bundled with CodePilot."""

    name: str = "codepilot"
    description: str = "CodePilot 项目工作流智能体"
    prompt: str = ""
    model: str = ""


@dataclass
class OpenCodeCommandConfig:
    """Bundled OpenCode command template."""

    description: str = ""
    template: str = ""
    agent: str = ""
    model: str = ""


@dataclass
class OpenCodeConfig:
    """CodePilot tool-level OpenCode profile config."""

    profile: OpenCodeProfileConfig = field(default_factory=OpenCodeProfileConfig)
    agent: OpenCodeAgentConfig = field(default_factory=OpenCodeAgentConfig)
    commands: dict[str, OpenCodeCommandConfig] = field(default_factory=dict)
    agent_language: str = "en"
    permission_mode: str = "ask"
    permissions: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    small_model: str = ""
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)


def default_opencode_config() -> OpenCodeConfig:
    return OpenCodeConfig()
