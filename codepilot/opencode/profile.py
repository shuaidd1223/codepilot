"""Generate CodePilot-managed OpenCode runtime profile files."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codepilot.core.config import normalize_agent_input_language, normalize_agent_output_language
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
    data_base_path: str | Path | None = None,
) -> OpenCodeProfilePlan:
    config = config or default_opencode_config()
    # *base* is the user-global runtime root where tool-generated files live
    # (tui.json, agents/, instructions/, commands/, plugins/).  Only
    # opencode.json may optionally be written to a separate project-level
    # path so users can see and edit their permission settings — the same
    # pattern as .claude/settings.json.
    base = Path(base_path) if base_path is not None else opencode_runtime_root()
    # XDG data / cache / state always live in the user-global runtime root.
    data_base = Path(data_base_path) if data_base_path is not None else base
    resolved_config_path = Path(config_path) if config_path is not None else base / "opencode.json"
    # tui.json, config/ etc. always stay under *base* — they are CodePilot
    # tool internals, not project-editable artifacts.
    tui_path = base / "tui.json"
    config_dir = base / "config"

    agent_name = _agent_name(config)
    commands = _commands(config, agent_name)
    language = normalize_agent_output_language(getattr(config, "agent_output_language", "zh-CN"))
    # instructions 文件路径解析（OpenCode 源码 packages/opencode/src/session/instruction.ts）：
    # - 相对路径：通过 globUp() 从 CWD（项目根目录）向上搜索，不会到 config 目录下找
    # - 绝对路径：glob(basename, {cwd: dirname}) 直接读取
    # - `~/` 前缀：展开为 $HOME 后按绝对路径处理
    # 所以必须用绝对路径，不能依赖相对 path。
    instructions_file = "codepilot.zh-CN.md" if language == "zh-CN" else "codepilot.en.md"
    instructions_path = str((config_dir / "instructions" / instructions_file).resolve())
    payload: dict[str, Any] = {
        "$schema": OPENCODE_CONFIG_SCHEMA,
        "mcp": _opencode_servers(normalize_mcp_servers(mcp_servers)),
        "default_agent": config.profile.default_agent or agent_name,
        "agent": {agent_name: _agent_payload(config, agent_name)},
        "command": commands,
        "permission": _permission_payload(config),
        "tools": _default_tools(),
        "instructions": [instructions_path],
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
            "input_paste": "ctrl+v,shift+insert",
        },
        "plugin": ["./config/tui-plugins/codepilot-brand.tsx"],
    }

    files = {
        str(resolved_config_path): _json_text(payload),
        str(tui_path): _json_text(tui_payload),
        str(config_dir / "agents" / f"{agent_name}.md"): _agent_markdown(config, agent_name),
        str(config_dir / "instructions" / instructions_file): _runtime_instructions_markdown(
            config.profile.brand_name,
            language=language,
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
            "XDG_DATA_HOME": str(data_base / "xdg-data"),
            "XDG_CACHE_HOME": str(data_base / "xdg-cache"),
            "XDG_STATE_HOME": str(data_base / "xdg-state"),
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
    # 语言与展示规则由 instructions 配置文件维护，
    # agent prompt 只保留功能描述，不重复注入语言指令。
    output_language = normalize_agent_output_language(config.agent_output_language)
    input_language = normalize_agent_input_language(config.agent_input_language)
    prompt = config.agent.prompt or _default_agent_prompt(config.profile.brand_name, language=input_language)
    payload: dict[str, Any] = {
        "description": config.agent.description or _default_agent_description(config.profile.brand_name, language=output_language),
        "prompt": prompt,
    }
    if config.agent.model:
        payload["model"] = config.agent.model
    return payload


def _agent_markdown(config: OpenCodeConfig, agent_name: str) -> str:
    output_language = normalize_agent_output_language(config.agent_output_language)
    input_language = normalize_agent_input_language(config.agent_input_language)
    prompt = config.agent.prompt or _default_agent_prompt(config.profile.brand_name, language=input_language)
    return (
        f"# {agent_name}\n\n"
        f"{config.agent.description or _default_agent_description(config.profile.brand_name, language=output_language)}\n\n"
        f"{prompt}\n"
    )


def _default_agent_description(brand_name: str, *, language: str = "en") -> str:
    brand = str(brand_name or "CodePilot").strip() or "CodePilot"
    if normalize_agent_output_language(language) == "en":
        return f"{brand} project workflow agent"
    return f"{brand} 项目工作流智能体"

def _default_agent_prompt(brand_name: str, *, language: str = "en") -> str:
    if normalize_agent_output_language(language) == "en":
        return (
            f"You are {brand_name}, a project workflow agent running inside OpenCode. "
            "Prefer CodePilot MCP tools for ALL operations. As a workflow agent, you MUST be proactive:\n\n"
            "1. BEFORE implementing any task, you MUST read:\n"
            "   - memory_events and note_show to understand past decisions and known pitfalls\n"
            "   - wiki_query when build/test/architecture conventions are involved\n"
            "2. BEFORE splitting tasks, check workflow_status to understand the current project state\n"
            "3. PREFERRED: For any requirement/task from the user, use the codepilot_pipeline tool:\n"
            "   - Pass the full requirement text as the 'requirement' parameter\n"
            "   - The pipeline handles plan -> execute -> workflow advance in one call\n"
            "   - This is the ONE-CALL way to get work done. Always prefer it.\n"
            "4. When pipeline is not suitable (e.g. you only need to execute, not plan), use:\n"
            "   - run_once to execute tasks from the backlog\n"
            "   - workflow_next with auto=true to advance the workflow\n"
            "5. AFTER completing tasks, write key findings to note_add or wiki_add for future tasks\n"
            "6. Use raw shell commands ONLY when MCP tools cannot cover the need\n"
            "7. When the user activates /task mode: you are in TOOL-ONLY mode. Do NOT read/write/edit files directly. "
            "Use ONLY CodePilot MCP tools: pipeline, run_once, workflow_next, workflow_status, create_task, generate_breakdown. "
            "Read memory_events and note_show, then call pipeline(requirement=...) to execute. "
            "Monitor execution results and report back. Do NOT implement code yourself.\n"
            "8. Check daemon_status periodically — if daemon is NOT running, drain the backlog yourself with run_once."
        )
    return (
        f"你是 {brand_name}，运行在 OpenCode 内的项目工作流智能体。"
        "作为工作流智能体，你必须主动使用以下工具，不要等待用户提醒：\n\n"
        "1. 优先使用 CodePilot MCP 工具完成所有操作。以下是必须主动读取的记忆：\n"
        "   - memory_events 和 note_show：在实现任何任务之前必须查询，了解过去决策和已知陷阱\n"
        "   - wiki_query：涉及构建/测试/架构时必须查询\n"
        "2. 任务拆分前，先查看 workflow_status 了解当前项目状态和待办事项\n"
        "3. 首选方式：收到用户的任何需求/任务时，直接调用 codepilot_pipeline 工具：\n"
        "   - 将完整需求文本作为 requirement 参数传入\n"
        "   - 该工具一键完成 规划→执行→工作流推进 全流程\n"
        "   - 这是最高效的工作方式，始终优先使用\n"
        "4. 当 pipeline 不适用时（如只需执行不需规划），使用：\n"
        "   - run_once 从 backlog 中执行任务\n"
        "   - workflow_next（auto=true）推进工作流\n"
        "5. 完成任务后，必须将关键发现写入 note_add 或 wiki_add，以便后续任务受益\n"
        "6. 只有在 MCP 工具完全无法覆盖需求时，才谨慎使用 shell 命令\n"
        "7. 当用户激活 /task 模式时：你处于纯工具模式。不要直接读写/编辑文件。"
        "只使用 CodePilot MCP 工具：pipeline、run_once、workflow_next、workflow_status、create_task、generate_breakdown。"
        "先读取 memory_events 和 note_show，然后调用 pipeline(requirement=...) 执行。"
        "你的角色是监听执行过程和结果并汇报，不要自己动手写代码。\n"
        "8. 定期检查 daemon_status——如果 daemon 未运行，手动使用 run_once 清空 backlog。不要等待用户提醒你执行任务。"
    )


def _runtime_instructions_content(brand_name: str, *, language: str = "en") -> str:
    """生成 instructions 配置文件内容。

    语言与展示规则通过 OpenCode 配置文件的 ``instructions`` 字段加载，
    不是注入到 agent prompt 中。这样 OpenCode 会将其作为系统级指令在
    每次模型调用时发送，不受 agent prompt 工程方式的影响。
    """
    brand = str(brand_name or "CodePilot").strip() or "CodePilot"
    if normalize_agent_output_language(language) == "en":
        return (
            "# Language and Display Rules\n"
            "\n"
            "## General\n"
            f"You are `{brand}`. All interactive output must be written in English. "
            "Regardless of the language used internally for reasoning, final answers, status notes, "
            "tool-call explanations, error explanations, permission request reasons, and thinking/reasoning summaries must be English.\n"
            "\n"
            "## Thinking / Reasoning\n"
            "- If OpenCode displays thinking / reasoning / analysis content, output an English summary and do not output Chinese.\n"
            "- Avoid Chinese headings or sentences in visible reasoning summaries.\n"
            "\n"
            "## Output Content\n"
            "- Code identifiers, commands, file paths, API names, MCP tool names, and raw error codes may remain as-is.\n"
            "- When requesting permission, explain in English what you will do, why it is needed, and what risk it carries.\n"
            "\n"
            "## Paste Hint\n"
            "If the user asks how to paste into the input box, explain these options:\n"
            "- `Shift+Insert` — most universal and supported by nearly every terminal\n"
            "- `Ctrl+Shift+V` — supported by most modern terminals\n"
            "- Right click — paste in Windows Terminal by default\n"
            "- `Ctrl+V` requires releasing that shortcut in Windows Terminal settings before OpenCode can receive it"
        )
    return (
        "# 语言与展示规则\n"
        "\n"
        "## 总则\n"
        f"你是 `{brand}`，所有交互输出必须使用简体中文。"
        "无论你内部使用何种语言进行推理，最终呈现给用户的回答、状态说明、"
        "工具调用说明、错误解释、权限申请理由和思考/推理摘要都必须输出简体中文。\n"
        "\n"
        "## 思考/推理过程\n"
        "- 如果 OpenCode 界面展示 thinking / reasoning / analysis 内容，必须输出中文摘要，不要输出英文。\n"
        "- 禁止使用 `Thinking`、`Reasoning`、`Analysis`、`I need to...` 等英文标题句式。\n"
        "- 如果模型支持显式 reasoning/thinking 输出，内部推理语言必须使用中文。\n"
        "\n"
        "## 输出内容\n"
        "- 代码标识符、命令、文件路径、API 名称、MCP tool 名称和原始错误码可保留原文，"
        "但解释这些内容时必须使用中文。\n"
        "- 需要用户确认权限时，用中文说明准备执行什么、为什么需要执行、风险是什么；"
        "不要在权限说明里输出英文提示语。\n"
        "- 如果引用英文资料或英文错误输出，优先用中文转述，只保留必要的原文片段。\n"
        "\n"
        "## 绝对禁止\n"
        "- 禁止输出英文标题（如 \"Analysis\"、\"Reasoning\"、\"Plan\"、\"Summary\"），必须使用中文对应标题。\n"
        "- 禁止在工具调用描述、任务状态说明中使用英文。\n"
        "\n"
        "## 输入粘贴提示\n"
        "如果用户询问如何在输入框中粘贴内容，告知以下方式：\n"
        "- `Shift+Insert` — 最通用，任何终端都支持\n"
        "- `Ctrl+Shift+V` — 大多数现代终端支持\n"
        "- 右键点击 — 粘贴（Windows Terminal 默认支持）\n"
        "- `Ctrl+V` 需要在 Windows Terminal 设置中释放此快捷键后才能透传到 OpenCode"
    )


def _runtime_instructions_markdown(brand_name: str, *, language: str = "en") -> str:
    title = "中文交互规则" if normalize_agent_output_language(language) == "zh-CN" else "English Interaction Rules"
    return f"# {brand_name or 'CodePilot'} {title}\n\n{_runtime_instructions_content(brand_name, language=language)}\n"


def _commands(config: OpenCodeConfig, agent_name: str) -> dict[str, dict[str, str]]:
    output_language = normalize_agent_output_language(config.agent_output_language)
    commands = _default_commands(agent_name, language=output_language)
    for name, raw in config.commands.items():
        command_name = str(name).strip()
        if not command_name:
            continue
        commands[command_name] = _command_payload(raw, agent_name, language=output_language)
    return commands


def _command_payload(raw: OpenCodeCommandConfig, default_agent: str, *, language: str = "en") -> dict[str, str]:
    english = normalize_agent_output_language(language) == "en"
    payload = {
        "description": raw.description or ("CodePilot workflow command" if english else "CodePilot 工作流命令"),
        "template": _with_chinese_command_instruction(
            raw.template or raw.description or "请使用 CodePilot MCP 工具完成这个工作流。"
        ) if not english else _with_english_command_instruction(
            raw.template or raw.description or "Use CodePilot MCP tools to complete this workflow."
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


def _with_english_command_instruction(template: str) -> str:
    body = str(template or "").strip()
    suffix = "Use English throughout; command names, file paths, code identifiers, and raw error codes may remain as-is."
    if suffix in body:
        return body
    if body:
        return f"{body}\n\n{suffix}"
    return suffix


def _default_commands(agent_name: str, *, language: str = "en") -> dict[str, dict[str, str]]:
    if normalize_agent_output_language(language) == "en":
        return {
            "Task Status": {
                "description": "View the current CodePilot task status.",
                "template": "List current CodePilot tasks and summarize backlog, in-progress, failed, and risky work in English.",
                "agent": agent_name,
            },
            "Create Task": {
                "description": "Create CodePilot tasks from the current requirement.",
                "template": "Create CodePilot tasks from the user's requirement. Use English throughout; ask follow-up questions only when critical details are missing.",
                "agent": agent_name,
            },
            "Fix Failed": {
                "description": "Inspect failed CodePilot tasks and plan the smallest repair.",
                "template": "Find failed CodePilot tasks, inspect the selected failure reason, and give the smallest controlled repair plan in English.",
                "agent": agent_name,
            },
            "Project Inspect": {
                "description": "Run a read-only CodePilot project inspection workflow.",
                "template": "Use CodePilot inspection and context tools to summarize actionable project risks in English without modifying files.",
                "agent": agent_name,
            },
        }
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
