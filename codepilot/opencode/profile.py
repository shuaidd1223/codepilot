"""Generate CodePilot-managed OpenCode runtime profile files."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codepilot.mcp.launchers import MCPServerSpec, normalize_mcp_servers
from codepilot.opencode.config import OpenCodeCommandConfig, OpenCodeConfig, default_opencode_config
from codepilot.opencode.paths import opencode_runtime_root


OPENCODE_CONFIG_SCHEMA = "https://opencode.ai/config.json"
OPENCODE_TUI_SCHEMA = "https://opencode.ai/tui.json"


@dataclass(frozen=True)
class OpenCodeProfilePlan:
    env: dict[str, str]
    files: dict[str, str]
    config: dict[str, Any]
    tui_config: dict[str, Any]


def build_opencode_profile(
    config: OpenCodeConfig | None = None,
    *,
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None,
    base_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> OpenCodeProfilePlan:
    config = config or default_opencode_config()
    base = Path(base_path) if base_path is not None else opencode_runtime_root()
    resolved_config_path = Path(config_path) if config_path is not None else base / "opencode.json"
    if config_path is not None:
        base = resolved_config_path.parent
    tui_path = base / "tui.json"
    config_dir = base / "config"

    agent_name = _agent_name(config)
    commands = _commands(config, agent_name)
    payload: dict[str, Any] = {
        "$schema": OPENCODE_CONFIG_SCHEMA,
        "mcp": _opencode_servers(normalize_mcp_servers(mcp_servers)),
        "default_agent": config.profile.default_agent or agent_name,
        "agent": {agent_name: _agent_payload(config, agent_name)},
        "command": commands,
        "permission": _permission_payload(config),
        "tools": _default_tools(),
        "instructions": ["AGENTS.md", "./config/instructions/codepilot.zh-CN.md"],
        "share": "disabled",
    }
    if config.providers:
        payload["provider"] = config.providers
    if config.model:
        payload["model"] = config.model
    if config.small_model:
        payload["small_model"] = config.small_model
    tui_payload: dict[str, Any] = {
        "$schema": OPENCODE_TUI_SCHEMA,
        "theme": config.profile.theme,
        "scroll_speed": int(config.profile.scroll_speed or 3),
        "diff_style": config.profile.diff_style,
        "mouse": bool(config.profile.mouse),
        "keybinds": {
            "app_exit": "ctrl+d,<leader>q",
            "input_clear": "ctrl+u",
        },
        "plugin": ["./config/tui-plugins/codepilot-brand.tsx"],
    }

    files = {
        str(resolved_config_path): _json_text(payload),
        str(tui_path): _json_text(tui_payload),
        str(config_dir / "agents" / f"{agent_name}.md"): _agent_markdown(config, agent_name),
        str(config_dir / "instructions" / "codepilot.zh-CN.md"): _runtime_instructions_markdown(
            config.profile.brand_name
        ),
        str(config_dir / "package.json"): _json_text(_tui_plugin_package()),
        str(config_dir / "tui-plugins" / "codepilot-brand.tsx"): _brand_tui_plugin(config.profile.brand_name),
    }
    for command_name, command in commands.items():
        template = str(command.get("template") or "")
        files[str(config_dir / "commands" / f"{command_name}.md")] = template.rstrip() + "\n"

    return OpenCodeProfilePlan(
        env={
            "OPENCODE_CONFIG": str(resolved_config_path),
            "OPENCODE_TUI_CONFIG": str(tui_path),
            "OPENCODE_CONFIG_DIR": str(config_dir),
            "XDG_DATA_HOME": str(base / "xdg-data"),
            "XDG_CACHE_HOME": str(base / "xdg-cache"),
            "XDG_STATE_HOME": str(base / "xdg-state"),
            "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
            "CODEPILOT_OPENCODE_BRAND_NAME": config.profile.brand_name,
        },
        files=files,
        config=payload,
        tui_config=tui_payload,
    )


def _agent_name(config: OpenCodeConfig) -> str:
    return (config.agent.name or config.profile.default_agent or "codepilot").strip() or "codepilot"


def _agent_payload(config: OpenCodeConfig, agent_name: str) -> dict[str, Any]:
    prompt = _with_chinese_runtime_instructions(
        config.agent.prompt or _default_agent_prompt(config.profile.brand_name),
        config.profile.brand_name,
    )
    payload: dict[str, Any] = {
        "description": config.agent.description or f"{config.profile.brand_name} 项目工作流智能体",
        "prompt": prompt,
    }
    if config.agent.model:
        payload["model"] = config.agent.model
    return payload


def _agent_markdown(config: OpenCodeConfig, agent_name: str) -> str:
    prompt = _with_chinese_runtime_instructions(
        config.agent.prompt or _default_agent_prompt(config.profile.brand_name),
        config.profile.brand_name,
    )
    return (
        f"# {agent_name}\n\n"
        f"{config.agent.description or f'{config.profile.brand_name} 项目工作流智能体'}\n\n"
        f"{prompt}\n"
    )


def _default_agent_prompt(brand_name: str) -> str:
    return (
        f"你是 {brand_name}，运行在 OpenCode 内的项目工作流智能体。"
        "优先使用 CodePilot MCP 工具查看任务状态、创建任务、巡检项目、执行失败修复闭环、触发 Hook "
        "以及完成受控工作流操作；只有在 MCP 能力无法覆盖时，才谨慎使用原始 shell 命令。"
    )


def _with_chinese_runtime_instructions(prompt: str, brand_name: str) -> str:
    body = str(prompt or "").strip()
    instructions = _chinese_runtime_instructions(brand_name)
    if instructions in body:
        return body
    if body:
        return f"{body}\n\n{instructions}"
    return instructions


def _chinese_runtime_instructions(brand_name: str) -> str:
    brand = str(brand_name or "CodePilot").strip() or "CodePilot"
    return (
        "## 语言与展示规则\n"
        f"- 你对用户呈现的身份是 `{brand}`，所有面向用户的回答、状态说明、工具调用说明、错误解释、"
        "权限申请理由和思考/推理摘要都必须使用简体中文。\n"
        "- 如果 OpenCode 界面展示 thinking / reasoning / analysis 内容，只输出中文摘要；"
        "不要使用 `Thinking`、`Reasoning`、`Analysis`、`I need to...` 这类英文标题或句式。\n"
        "- 代码标识符、命令、文件路径、API 名称、MCP tool 名称和原始错误码可以保留英文；"
        "解释这些内容时使用中文。\n"
        "- 需要用户确认权限时，用中文说明准备执行什么、为什么需要执行、风险是什么；"
        "不要在权限说明里输出英文提示语。\n"
        "- 如果引用英文资料或英文错误输出，优先用中文转述，只保留必要的原文片段。"
    )


def _runtime_instructions_markdown(brand_name: str) -> str:
    return f"# {brand_name or 'CodePilot'} 中文交互规则\n\n{_chinese_runtime_instructions(brand_name)}\n"


def _commands(config: OpenCodeConfig, agent_name: str) -> dict[str, dict[str, str]]:
    commands = _default_commands(agent_name)
    for name, raw in config.commands.items():
        command_name = str(name).strip()
        if not command_name:
            continue
        commands[command_name] = _command_payload(raw, agent_name)
    return commands


def _command_payload(raw: OpenCodeCommandConfig, default_agent: str) -> dict[str, str]:
    payload = {
        "description": raw.description or "CodePilot 工作流命令",
        "template": _with_chinese_command_instruction(
            raw.template or raw.description or "请使用 CodePilot MCP 工具完成这个工作流。"
        ),
        "agent": raw.agent or default_agent,
    }
    if raw.model:
        payload["model"] = raw.model
    return payload


def _with_chinese_command_instruction(template: str) -> str:
    body = str(template or "").strip()
    suffix = "请全程使用简体中文输出；命令名、文件路径、代码标识符和原始错误码可保留原文。"
    if suffix in body:
        return body
    if body:
        return f"{body}\n\n{suffix}"
    return suffix


def _default_commands(agent_name: str) -> dict[str, dict[str, str]]:
    return {
        "任务状态": {
            "description": "查看当前 CodePilot 任务状态。",
            "template": "列出当前 CodePilot 任务，并用简体中文汇总待办、执行中、失败和有风险的工作。",
            "agent": agent_name,
        },
        "创建任务": {
            "description": "根据当前需求创建 CodePilot 任务。",
            "template": "根据用户需求创建 CodePilot 任务。全程使用简体中文；只在缺少关键细节时追问。",
            "agent": agent_name,
        },
        "修复失败": {
            "description": "检查失败的 CodePilot 任务并规划最小修复。",
            "template": "找到失败的 CodePilot 任务，检查选中的失败原因，并用简体中文给出最小可控修复方案。",
            "agent": agent_name,
        },
        "项目巡检": {
            "description": "运行只读 CodePilot 项目巡检工作流。",
            "template": "使用 CodePilot 巡检和上下文工具用简体中文总结可执行的项目风险，不要修改文件。",
            "agent": agent_name,
        },
    }


def _default_permissions() -> dict[str, str]:
    return {
        "*": "ask",
        "bash": "ask",
        "edit": "ask",
        "write": "ask",
        "webfetch": "ask",
    }


def _permission_payload(config: OpenCodeConfig) -> str | dict[str, Any]:
    mode = str(config.permission_mode or "ask").strip().lower().replace("-", "_")
    if mode in {"full", "full_access", "allow", "allow_all"}:
        return "allow"
    if mode == "custom":
        permissions = _default_permissions()
        permissions.update({str(key): value for key, value in (config.permissions or {}).items()})
        return permissions
    return _default_permissions()


def _default_tools() -> dict[str, bool]:
    return {
        "bash": True,
        "edit": True,
        "write": True,
        "webfetch": True,
    }


def _tui_plugin_package() -> dict[str, Any]:
    return {
        "private": True,
        "type": "module",
        "dependencies": {
            "@opentui/core": "^0.1.93",
            "@opentui/solid": "^0.1.93",
            "solid-js": "1.9.12",
        },
    }


def _brand_tui_plugin(brand_name: str) -> str:
    brand = brand_name.strip() or "CodePilot"
    brand_literal = json.dumps(brand, ensure_ascii=False)
    logo_literal = json.dumps(_brand_logo_lines(brand), ensure_ascii=False, indent=2)
    return f"""\
/** @jsxImportSource @opentui/solid */
import type {{ TuiPlugin }} from "@opencode-ai/plugin/tui"

const brandName = {brand_literal}
const logoLines = {logo_literal}
const id = "codepilot-brand"

const tui: TuiPlugin = async (api) => {{
  api.slots.register({{
    order: 1000,
    slots: {{
      home_logo(ctx) {{
        const theme = ctx.theme.current
        return (
          <box alignItems="center" flexDirection="column" gap={{1}}>
            <box alignItems="center" flexDirection="column" gap={{0}}>
              {{logoLines.map((line) => (
                <text fg={{theme.success}}>
                  <b>{{line}}</b>
                </text>
              ))}}
            </box>
            <box alignItems="center" flexDirection="column" gap={{0}}>
              <text fg={{theme.text}}>
                <b>{{brandName}}</b>
              </text>
              <text fg={{theme.textMuted}}>MCP 工具已接入 · 项目工作流智能体</text>
            </box>
          </box>
        )
      }},
      sidebar_title(ctx, props) {{
        const theme = ctx.theme.current
        return (
          <box flexDirection="column" paddingRight={{1}}>
            <text fg={{theme.success}}>
              <b>{{props.title || brandName}}</b>
            </text>
            <text fg={{theme.textMuted}}>项目工作流</text>
          </box>
        )
      }},
      sidebar_footer(ctx) {{
        const theme = ctx.theme.current
        return (
          <box flexShrink={{0}}>
            <text fg={{theme.textMuted}}>
              <span style={{{{ fg: theme.success }}}}>•</span> <b>{{brandName}}</b> <span>{{api.app.version}}</span>
            </text>
          </box>
        )
      }},
    }},
  }})
}}

export default {{ id, tui }}
"""


def _brand_logo_lines(brand_name: str) -> list[str]:
    return [
        "  _____ ____  _____  ______ _____ _____ _      ____ _______",
        " / ____/ __ \\|  __ \\|  ____|  __ \\_   _| |    / __ \\__   __|",
        "| |   | |  | | |  | | |__  | |__) || | | |   | |  | | | |",
        "| |   | |  | | |  | |  __| |  ___/ | | | |   | |  | | | |",
        "| |___| |__| | |__| | |____| |    _| |_| |___| |__| | | |",
        " \\_____\\____/|_____/|______|_|   |_____|______\\____/  |_|",
    ]


def _opencode_servers(servers: list[MCPServerSpec]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.url:
            item: dict[str, Any] = {
                "type": "remote",
                "url": server.url,
                "enabled": server.enabled,
            }
            if server.headers:
                item["headers"] = dict(server.headers)
        else:
            item = {
                "type": "local",
                "command": [server.command, *server.args],
                "enabled": server.enabled,
            }
            if server.env:
                item["environment"] = dict(server.env)
        result[server.name] = item
    return result


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
