"""AGENTS.toml maintenance commands."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import click

from codepilot.core import config as config_mod
from codepilot.core.gitignore import ensure_gitignore_entry


TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(value: Any) -> str:
    key = str(value)
    return key if TOML_BARE_KEY_RE.match(key) else _quote(key)


def _quote(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return _quote("" if value is None else str(value))


def _string(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _choice(value: Any, choices: set[str], default: str) -> str:
    text = str(value).strip().lower() if isinstance(value, str) else ""
    return text if text in choices else default


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_non_negative_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _agent_commands(raw: Any) -> dict[str, str]:
    """Canonicalize the [agents.commands] map, applying defaults for missing families."""
    merged = dict(config_mod.DEFAULT_AGENT_COMMANDS)
    if isinstance(raw, dict):
        for family, value in raw.items():
            family_name = str(family).strip()
            if not family_name:
                continue
            cmd = _string(value, "").strip()
            if cmd:
                merged[family_name] = cmd
    return merged


def _fallback_cli_order(raw: Any) -> list[str]:
    """Canonicalize automation.fallback_cli_order. String -> 1-element list. Empty -> defaults."""
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else list(config_mod.DEFAULT_FALLBACK_CLI_ORDER)
    if isinstance(raw, (list, tuple)):
        cleaned = [str(item).strip() for item in raw if str(item or "").strip()]
        return cleaned or list(config_mod.DEFAULT_FALLBACK_CLI_ORDER)
    return list(config_mod.DEFAULT_FALLBACK_CLI_ORDER)


def _string_list(raw: Any, default: list[str]) -> list[str]:
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else list(default)
    if isinstance(raw, (list, tuple)):
        cleaned = [str(item).strip() for item in raw if str(item or "").strip()]
        return cleaned
    return list(default)


def _preflight_dirty_worktree(raw: Any) -> str:
    try:
        return config_mod.normalize_preflight_dirty_worktree(raw)
    except config_mod.ConfigError:
        return "stop"


def _opencode_permission(raw: Any) -> dict[str, Any]:
    opencode = raw if isinstance(raw, dict) else {}
    permission = opencode.get("permission") if isinstance(opencode.get("permission"), dict) else {}
    mode = _choice(
        permission.get("mode"),
        {"ask", "manual", "manual_confirm", "confirm", "full", "full_access", "allow", "allow_all", "custom"},
        "ask",
    )
    if mode in {"manual", "manual_confirm", "confirm"}:
        mode = "ask"
    if mode in {"full", "allow", "allow_all"}:
        mode = "full_access"

    canonical: dict[str, Any] = {"mode": mode}
    for source in (permission, permission.get("rules") if isinstance(permission.get("rules"), dict) else {}):
        for key, value in source.items():
            key_text = str(key).strip()
            if key_text in {"mode", "rules"} or not key_text:
                continue
            normalized = _opencode_permission_value(value)
            if normalized is not None:
                canonical[key_text] = normalized
    return {"permission": canonical}


def _opencode_permission_value(value: Any) -> str | dict[str, Any] | None:
    if isinstance(value, dict):
        nested: dict[str, Any] = {}
        for nested_key, nested_value in value.items():
            key_text = str(nested_key).strip()
            if not key_text:
                continue
            normalized = _opencode_permission_value(nested_value)
            if normalized is not None:
                nested[key_text] = normalized
        return nested or None
    value_text = str(value).strip().lower()
    return value_text if value_text in {"ask", "allow", "deny"} else None


def _scheduled_agents(raw: Any) -> dict[str, dict[str, Any]]:
    agents = raw if isinstance(raw, dict) else {}
    canonical: dict[str, dict[str, Any]] = {}
    for name in sorted(agents):
        cfg = agents.get(name)
        if not isinstance(cfg, dict):
            continue
        agent = _string(cfg.get("agent"), "").strip()
        prompt = _string(cfg.get("prompt"), "").strip()
        interval = _optional_string(cfg.get("interval"))
        schedule = _optional_string(cfg.get("schedule"))
        if not agent or not prompt or (not interval and not schedule):
            continue
        item: dict[str, Any] = {
            "enabled": _bool(cfg.get("enabled"), True),
            "agent": agent,
        }
        if interval:
            item["interval"] = interval
        if schedule:
            item["schedule"] = schedule
        item["prompt"] = prompt
        max_cost = _optional_non_negative_float(cfg.get("max_cost_usd"))
        max_daily_cost = _optional_non_negative_float(cfg.get("max_daily_cost_usd"))
        if max_cost is not None:
            item["max_cost_usd"] = max_cost
        if max_daily_cost is not None:
            item["max_daily_cost_usd"] = max_daily_cost
        canonical[str(name)] = item
    return canonical


def _event_agents(raw: Any) -> dict[str, dict[str, Any]]:
    agents = raw if isinstance(raw, dict) else {}
    canonical: dict[str, dict[str, Any]] = {}
    for name in sorted(agents):
        cfg = agents.get(name)
        if not isinstance(cfg, dict):
            continue
        trigger = _optional_string(cfg.get("trigger"))
        agent = _string(cfg.get("agent"), "").strip()
        prompt = _string(cfg.get("prompt"), "").strip()
        if not trigger or not agent or not prompt:
            continue
        item: dict[str, Any] = {
            "enabled": _bool(cfg.get("enabled"), True),
            "trigger": trigger,
            "agent": agent,
            "prompt": prompt,
        }
        max_cost = _optional_non_negative_float(cfg.get("max_cost_usd"))
        max_daily_cost = _optional_non_negative_float(cfg.get("max_daily_cost_usd"))
        if max_cost is not None:
            item["max_cost_usd"] = max_cost
        if max_daily_cost is not None:
            item["max_daily_cost_usd"] = max_daily_cost
        canonical[str(name)] = item
    return canonical


def _supported_provider(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bool):
        return {
            "enabled": raw,
            "api_key": "",
            "model": "",
            "base_url": "",
            "max_tokens": 4096,
            "temperature": 0.7,
            "auto_model_selection": False,
            "simple_model": "",
            "complex_model": "",
            "thinking": "",
            "reasoning_effort": "",
        }
    if not isinstance(raw, dict):
        return None
    return {
        "enabled": _bool(raw.get("enabled"), True),
        "api_key": _string(raw.get("api_key"), ""),
        "model": _string(raw.get("model"), ""),
        "base_url": _string(raw.get("base_url"), ""),
        "max_tokens": _int(raw.get("max_tokens"), 4096, min_value=1),
        "temperature": _float(raw.get("temperature"), 0.7),
        "auto_model_selection": _bool(raw.get("auto_model_selection"), False),
        "simple_model": _string(raw.get("simple_model"), ""),
        "complex_model": _string(raw.get("complex_model"), ""),
        "thinking": _choice(raw.get("thinking"), {"", "auto", "enabled", "disabled"}, ""),
        "reasoning_effort": _choice(raw.get("reasoning_effort"), {"", "auto", "high", "max"}, ""),
    }


def _load_toml_dict(path: Path) -> dict[str, Any]:
    """Load TOML file and return as dict.

    Missing optional overlay files are treated as empty; invalid TOML still
    raises so callers can report real configuration errors.
    """
    import tomllib

    if not path.exists():
        return {}

    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def _canonical_secrets(data: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {"providers": {}, "feishu_bot": {}}

    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    for name in sorted(providers):
        cfg = providers.get(name)
        if not isinstance(cfg, dict):
            continue
        api_key = str(cfg.get("api_key", "") or "").strip()
        if api_key:
            canonical["providers"][str(name)] = {"api_key": api_key}

    feishu_bot = data.get("feishu_bot") if isinstance(data.get("feishu_bot"), dict) else {}
    app_secret = str(feishu_bot.get("app_secret", "") or "").strip()
    if app_secret:
        canonical["feishu_bot"] = {"app_secret": app_secret}

    return canonical


def _extract_sync_secrets(data: dict[str, Any]) -> dict[str, Any]:
    feishu_bot = data.get("feishu_bot") if isinstance(data.get("feishu_bot"), dict) else {}
    app_secret = str(feishu_bot.get("app_secret", "") or "").strip()
    if not app_secret:
        return {}
    return {"feishu_bot": {"app_secret": app_secret}}


def _merge_secret_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in set(base) | set(override):
        base_value = base.get(key)
        override_value = override.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = _merge_secret_dicts(base_value, override_value)
        elif key in override:
            merged[key] = override_value
        else:
            merged[key] = base_value
    return merged


def _canonical_config(data: dict[str, Any], *, project_name: str) -> dict[str, Any]:
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    shell = data.get("shell") if isinstance(data.get("shell"), dict) else {}
    agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    dispatch = data.get("dispatch") if isinstance(data.get("dispatch"), dict) else {}
    automation = data.get("automation") if isinstance(data.get("automation"), dict) else {}
    inspect = data.get("inspect") if isinstance(data.get("inspect"), dict) else {}
    opencode = data.get("opencode") if isinstance(data.get("opencode"), dict) else {}
    notifications = data.get("notifications") if isinstance(data.get("notifications"), dict) else {}
    feishu_bot = data.get("feishu_bot") if isinstance(data.get("feishu_bot"), dict) else {}
    scheduled_agents = _scheduled_agents(automation.get("scheduled_agents"))
    event_agents = _event_agents(automation.get("event_agents"))

    signals = inspect.get("signals", ["git_log", "failed_tasks", "todos"])
    if not isinstance(signals, (list, tuple)) or not all(isinstance(item, str) for item in signals):
        signals = ["git_log", "failed_tasks", "todos"]

    canonical: dict[str, Any] = {
        "project": {
            "name": _string(project.get("name"), project_name),
            "base_branch": _string(project.get("base_branch"), "dev"),
            "default_mode": _string(project.get("default_mode"), "dual"),
            "worktree_base": _string(project.get("worktree_base"), ""),
        },
        "shell": {
            "preferred": _choice(
                shell.get("preferred"),
                {"auto", "powershell", "pwsh", "powershell7", "bash", "zsh", "sh"},
                "auto",
            ),
            "powershell_path": _string(shell.get("powershell_path"), ""),
            "bash_path": _string(shell.get("bash_path"), ""),
        },
        "agents": {
            "commands": _agent_commands(agents.get("commands")),
            "planner": _string(agents.get("planner"), ""),
            "builder": _string(agents.get("builder"), ""),
            "reviewer": _string(agents.get("reviewer"), ""),
        },
        "dispatch": {
            "dispatch_path": _string(dispatch.get("dispatch_path"), ""),
            "interval_seconds": _int(dispatch.get("interval_seconds"), 600, min_value=1),
            "stale_minutes": _int(dispatch.get("stale_minutes"), 30, min_value=1),
        },
        "automation": {
            "planner": _string(automation.get("planner"), "codex"),
            "default_agent": _string(automation.get("default_agent"), ""),
            "task_agent": _string(automation.get("task_agent"), "dual"),
            "executor": _choice(automation.get("executor"), {"auto", "dispatch", "builtin"}, "builtin"),
            "auto_execute": _bool(automation.get("auto_execute"), True),
            "confirm_before_execute": _bool(automation.get("confirm_before_execute"), False),
            "auto_commit": _bool(automation.get("auto_commit"), True),
            "max_tasks": _int(automation.get("max_tasks"), 5, min_value=1, max_value=8),
            "max_retries": _int(automation.get("max_retries"), 3, min_value=0),
            "per_task_branch": _bool(automation.get("per_task_branch"), True),
            "task_workspace": _choice(automation.get("task_workspace"), {"direct", "branch", "worktree"}, "branch"),
            "worktree_context_patterns": _string_list(automation.get("worktree_context_patterns"), [".env*"]),
            "worktree_context_link_patterns": _string_list(
                automation.get("worktree_context_link_patterns"),
                [
                    "node_modules",
                    ".venv",
                    "venv",
                    "env",
                    ".tox",
                    ".nox",
                    ".gradle",
                    "target",
                    "build",
                    "cmake-build-*",
                    ".dart_tool",
                    "Pods",
                    "Carthage",
                    ".terraform",
                    ".serverless",
                ],
            ),
            "preflight_dirty_worktree": _preflight_dirty_worktree(automation.get("preflight_dirty_worktree")),
            "two_stage_planning": _bool(automation.get("two_stage_planning"), True),
            "clarify_vague_requirements": _bool(automation.get("clarify_vague_requirements"), True),
            "clarify_max_turns": _int(automation.get("clarify_max_turns"), 3, min_value=0),
            "max_review_rounds": _int(automation.get("max_review_rounds"), 2, min_value=1, max_value=5),
            "agent_silence_timeout_seconds": _int(
                automation.get("agent_silence_timeout_seconds"),
                0,
                min_value=0,
            ),
            "fallback_cli_order": _fallback_cli_order(automation.get("fallback_cli_order")),
            "agent_language": config_mod.normalize_agent_language(automation.get("agent_language")),
        },
        "inspect": {
            "enabled": _bool(inspect.get("enabled"), False),
            "interval_seconds": _int(inspect.get("interval_seconds"), 1800, min_value=1),
            "max_new_tasks_per_round": _int(inspect.get("max_new_tasks_per_round"), 3, min_value=1),
            "signals": tuple(signals),
            "auto_execute": _bool(inspect.get("auto_execute"), False),
            "priority": _choice(inspect.get("priority"), {"p0", "p1", "p2", "p3"}, "P3").upper(),
            "planner": _string(inspect.get("planner"), ""),
        },
        "opencode": _opencode_permission(opencode),
        "providers": {},
        "notifications": {
            "webhook_url": _string(notifications.get("webhook_url"), ""),
            "provider": _choice(notifications.get("provider"), {"auto", "feishu", "wecom", "generic"}, "auto"),
            "webhook_secret": _string(notifications.get("webhook_secret"), ""),
            "enabled": _bool(notifications.get("enabled"), False),
        },
        "feishu_bot": {
            "enabled": _bool(feishu_bot.get("enabled"), False),
            "app_id": _string(feishu_bot.get("app_id"), ""),
            "node_command": _string(feishu_bot.get("node_command"), "node"),
            "default_project": _string(feishu_bot.get("default_project"), ""),
            "command_prefix": _string(feishu_bot.get("command_prefix"), ""),
        },
    }

    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    for name in sorted(providers):
        provider_cfg = _supported_provider(providers[name])
        if provider_cfg is not None:
            canonical["providers"][str(name)] = provider_cfg

    if scheduled_agents:
        canonical["automation"]["scheduled_agents"] = scheduled_agents
    if event_agents:
        canonical["automation"]["event_agents"] = event_agents

    return canonical


SECTION_COMMENTS: dict[str, list[str]] = {
    "project": [
        "项目配置。name 用于 `codepilot -p/--project`；base_branch 是任务分支合并回去的主分支。",
    ],
    "shell": [
        "Shell 配置。preferred 留 auto 时自动检测；路径留空表示使用 PATH 中的命令。",
    ],
    "agents": [
        "本地 CLI Agent 配置。planner/builder/reviewer 留空时使用对应场景默认值。",
    ],
    "agents.commands": [
        "CLI family 命令映射。键为 family 名（claude/codex/opencode 等），值为命令或绝对路径。",
        "缺失项默认使用同名命令。",
    ],
    "opencode": [
        "项目级 OpenCode 配置。这里只保存项目权限策略；品牌、TUI、Agent 仍由 CodePilot 工具自身管理。",
    ],
    "opencode.permission": [
        "OpenCode 权限策略。ask=逐项确认；full_access=无需确认；custom=按规则配置。",
        "custom 可配置 `*`、bash、edit、write、webfetch 或 MCP 工具名，值为 ask / allow / deny。",
    ],
    "dispatch": [
        "外部 dispatch 执行器配置。builtin 执行器也会读取 stale_minutes 等通用超时语义。",
    ],
    "automation": [
        "自动规划与执行配置。",
        "task_workspace: direct=主工作区直接改；branch=主工作区临时分支；worktree=独立临时 worktree。",
        "preflight_dirty_worktree: stop=停止；commit=预检提交；stash=stash 并记录。",
    ],
    "inspect": [
        "定时/手动巡检配置。planner 留空时按 显式参数 > [agents].planner > codex 解析。",
    ],
    "providers": [
        "AI Provider API 配置。api_key 可留空改用环境变量；base_url/model 可接自定义兼容接口。",
    ],
    "notifications": [
        "通知配置。enabled=false 时 webhook_url 会被保留但不会发送。",
    ],
    "feishu_bot": [
        "飞书企业应用长连接机器人配置。运行时使用官方 Node SDK，不依赖 lark-cli。",
        f"App Secret 只写到同目录 {config_mod.SECRETS_FILENAME}，不要放在 AGENTS.toml。",
    ],
}

KEY_COMMENTS: dict[tuple[str, str], list[str]] = {
    ("project", "name"): ["项目名称，默认取 AGENTS.toml 所在目录名。"],
    ("project", "base_branch"): ["Git 主分支/基准分支。"],
    ("project", "default_mode"): ["兼容字段：默认任务智能体；自动规划执行优先使用 [automation].task_agent。"],
    ("project", "worktree_base"): ["worktree 隔离目录；留空时自动推导到 ~/.codepilot/data/<project>/worktrees/。"],
    ("shell", "preferred"): ["auto / powershell / pwsh / powershell7 / bash / zsh / sh。"],
    ("shell", "powershell_path"): ["自定义 PowerShell 可执行文件路径；留空表示自动查找。"],
    ("shell", "bash_path"): ["自定义 Bash 可执行文件路径；留空表示自动查找。"],
    ("agents", "planner"): ["通用规划器；留空时使用场景默认值。"],
    ("agents", "builder"): ["dual 模式 builder；留空时默认 codex。"],
    ("agents", "reviewer"): ["dual 模式 reviewer；留空时默认 claude。"],
    ("opencode.permission", "mode"): ["ask / full_access / custom。默认 ask。"],
    ("dispatch", "dispatch_path"): ["task-dispatch 脚本路径；留空时先查 ~/.codepilot/data/<project>/scripts/，再查包内置脚本。"],
    ("dispatch", "interval_seconds"): ["轮询间隔秒数。"],
    ("dispatch", "stale_minutes"): ["任务无心跳超过该分钟数后视为过期。"],
    ("automation", "planner"): ["自然语言需求默认规划智能体。"],
    ("automation", "default_agent"): ["chat 默认启动的 MCP agent；留空时使用 opencode。"],
    ("automation", "task_agent"): ["规划出的任务默认执行智能体；显式 --agent / UI 选择会覆盖它。"],
    ("automation", "executor"): ["auto / dispatch / builtin。"],
    ("automation", "auto_execute"): ["输入需求后是否自动开始执行。"],
    ("automation", "confirm_before_execute"): ["交互终端下规划完成后是否先确认。"],
    ("automation", "auto_commit"): ["内置执行器成功后是否自动提交。"],
    ("automation", "max_tasks"): ["复杂需求最多拆分出的子任务数。"],
    ("automation", "max_retries"): ["单个子任务失败后的最大重试次数。"],
    ("automation", "per_task_branch"): ["是否为任务创建独立执行分支；false 时按当前工作区执行。"],
    ("automation", "task_workspace"): ["direct / branch / worktree；缺失或非法值默认 branch。"],
    ("automation", "preflight_dirty_worktree"): ["stop / commit / stash；预检发现未提交改动时的处理策略，默认 stop。"],
    ("automation", "two_stage_planning"): ["规划前先侦察代码，再拆任务。"],
    ("automation", "clarify_vague_requirements"): ["需求模糊时先反问澄清。"],
    ("automation", "clarify_max_turns"): ["最多澄清轮数，达到后按当前信息规划。"],
    ("automation", "max_review_rounds"): ["Builder/Reviewer 闭环最大轮数；1 等于关闭闭环。"],
    ("automation", "agent_silence_timeout_seconds"): ["CLI 连续无输出多少秒后终止；0 表示关闭保护。"],
    ("automation", "fallback_cli_order"): ["文本模式 CLI 兜底顺序；前面项不可用时按顺序退到下一个。"],
    ("automation", "agent_language"): ["智能体 prompt / 任务内容 / 输出语言偏好：en 或 zh-CN；默认 en。"],
    ("inspect", "enabled"): ["是否启用 daemon 定时巡检。"],
    ("inspect", "interval_seconds"): ["巡检间隔秒数。"],
    ("inspect", "max_new_tasks_per_round"): ["每轮巡检最多新增候选任务数。"],
    ("inspect", "signals"): ["巡检输入信号，可包含 git_log、failed_tasks、todos、deps、ruff、pytest、code_metrics。"],
    ("inspect", "auto_execute"): ["巡检新增任务后是否自动执行。"],
    ("inspect", "priority"): ["巡检新增任务默认优先级。"],
    ("inspect", "planner"): ["巡检专用 planner；留空时回退到 [agents].planner / codex。"],
    ("notifications", "webhook_url"): ["任务状态通知 Webhook URL。"],
    ("notifications", "provider"): ["auto / feishu / wecom / generic。建议飞书显式写 feishu。"],
    ("notifications", "webhook_secret"): ["飞书机器人签名密钥；未开启签名校验时留空。"],
    ("notifications", "enabled"): ["是否发送通知。"],
    ("feishu_bot", "enabled"): ["是否启用飞书企业应用长连接机器人。"],
    ("feishu_bot", "app_id"): ["飞书企业应用 App ID。"],
    ("feishu_bot", "node_command"): ["Node.js 命令名或完整路径；默认 node。"],
    ("feishu_bot", "default_project"): ["未显式指定项目时默认操作的项目名。"],
    ("feishu_bot", "command_prefix"): ["可选命令前缀，例如 cp；留空则直接识别 help/tasks/stop 等命令。"],
}

PROVIDER_EXAMPLES: dict[str, dict[str, Any]] = {
    "openai-gpt4o": {
        "enabled": True,
        "model": "gpt-4o",
        "base_url": "",
        "api_key": "",
        "max_tokens": 4096,
        "temperature": 0.7,
    },
    "claude-sonnet": {
        "enabled": True,
        "model": "claude-sonnet-4-20250514",
        "base_url": "",
        "api_key": "",
        "max_tokens": 4096,
        "temperature": 0.7,
    },
    "hunyuan": {
        "enabled": False,
        "model": "hunyuan",
        "base_url": "https://hunyuan.cloud.tencent.com",
        "api_key": "",
        "max_tokens": 4096,
        "temperature": 0.7,
    },
    "qwen": {
        "enabled": False,
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "max_tokens": 4096,
        "temperature": 0.7,
    },
    "deepseek": {
        "enabled": False,
        "model": "",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "max_tokens": 8192,
        "temperature": 0.7,
        "auto_model_selection": True,
        "simple_model": "deepseek-v4-flash",
        "complex_model": "deepseek-v4-pro",
        "thinking": "auto",
        "reasoning_effort": "auto",
    },
    "ollama": {
        "enabled": False,
        "model": "llama3",
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "max_tokens": 4096,
        "temperature": 0.7,
    },
}


def _append_comments(lines: list[str], comments: list[str]) -> None:
    for comment in comments:
        lines.append(f"# {comment}")


def _emit_section(lines: list[str], title: str, values: dict[str, Any]) -> None:
    _append_comments(lines, SECTION_COMMENTS.get(title, []))
    lines.append(f"[{title}]")
    nested: list[tuple[str, dict[str, Any]]] = []
    for key, value in values.items():
        if isinstance(value, dict):
            nested.append((key, value))
            continue
        _append_comments(lines, KEY_COMMENTS.get((title, key), []))
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    lines.append("")
    for sub_key, sub_values in nested:
        _emit_subtable(lines, f"{title}.{sub_key}", sub_values)


def _emit_subtable(lines: list[str], title: str, values: dict[str, Any]) -> None:
    """Emit a nested TOML sub-table, e.g. [agents.commands]."""
    _append_comments(lines, SECTION_COMMENTS.get(title, []))
    lines.append(f"[{title}]")
    nested: list[tuple[str, dict[str, Any]]] = []
    for key, value in values.items():
        if isinstance(value, dict):
            nested.append((key, value))
            continue
        _append_comments(lines, KEY_COMMENTS.get((title, key), []))
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    lines.append("")
    for sub_key, sub_values in nested:
        _emit_subtable(lines, f"{title}.{_toml_key(sub_key)}", sub_values)


def _emit_provider_examples(lines: list[str], configured: set[str]) -> None:
    lines.append("# 常用 provider 示例。需要启用时取消对应小节注释；已配置的小节会在下方以实际值输出。")
    for name, values in PROVIDER_EXAMPLES.items():
        label = "已在下方配置" if name in configured else "示例"
        lines.append("")
        lines.append(f"# {name} ({label})")
        lines.append(f"# [providers.{name}]")
        for key, value in values.items():
            lines.append(f"# {key} = {_toml_value(value)}")
    lines.append("")


def _emit_provider_section(lines: list[str], name: str, values: dict[str, Any]) -> None:
    lines.append(f"[providers.{name}]")
    lines.append("# 是否启用该 provider 配置。")
    lines.append(f"enabled = {_toml_value(values.get('enabled', True))}")
    lines.append("# API Key；留空时读取 provider 对应环境变量。")
    lines.append(f"api_key = {_toml_value(values.get('api_key', ''))}")
    lines.append("# 模型名；用于自定义模型或兼容接口。")
    lines.append(f"model = {_toml_value(values.get('model', ''))}")
    lines.append("# 自定义接口地址；留空时使用 SDK/provider 默认地址。")
    lines.append(f"base_url = {_toml_value(values.get('base_url', ''))}")
    lines.append("# 单次响应最大 token 数。")
    lines.append(f"max_tokens = {_toml_value(values.get('max_tokens', 4096))}")
    lines.append("# 采样温度。")
    lines.append(f"temperature = {_toml_value(values.get('temperature', 0.7))}")
    lines.append("# 是否按任务难度自动切换模型。")
    lines.append(f"auto_model_selection = {_toml_value(values.get('auto_model_selection', False))}")
    lines.append("# 简单任务使用的模型；留空则使用 provider 默认模型。")
    lines.append(f"simple_model = {_toml_value(values.get('simple_model', ''))}")
    lines.append("# 复杂任务使用的模型；DeepSeek 推荐 deepseek-v4-pro。")
    lines.append(f"complex_model = {_toml_value(values.get('complex_model', ''))}")
    lines.append("# 思考模式：auto / enabled / disabled；非 DeepSeek provider 可留空。")
    lines.append(f"thinking = {_toml_value(values.get('thinking', ''))}")
    lines.append("# 思考强度：auto / high / max；thinking=enabled 时生效。")
    lines.append(f"reasoning_effort = {_toml_value(values.get('reasoning_effort', ''))}")
    lines.append("")


def render_agents_toml(canonical: dict[str, Any]) -> str:
    lines: list[str] = [
        "# CodePilot 项目配置文件",
        "# 由 `codepilot config sync` 生成/同步。",
        "",
    ]
    for section in ("project", "shell", "agents", "opencode", "dispatch", "automation", "inspect"):
        _emit_section(lines, section, canonical[section])

    _append_comments(lines, SECTION_COMMENTS["providers"])
    lines.append("[providers]")
    lines.append("")
    providers = canonical.get("providers", {})
    _emit_provider_examples(lines, set(providers))
    if providers:
        for name, values in providers.items():
            _emit_provider_section(lines, name, values)

    _emit_section(lines, "notifications", canonical["notifications"])
    _emit_section(lines, "feishu_bot", canonical["feishu_bot"])
    return "\n".join(lines).rstrip() + "\n"


def render_secrets_toml(canonical: dict[str, Any]) -> str:
    providers = canonical.get("providers", {}) if isinstance(canonical.get("providers"), dict) else {}
    feishu_bot = canonical.get("feishu_bot", {}) if isinstance(canonical.get("feishu_bot"), dict) else {}
    if not providers and not feishu_bot:
        return ""

    lines: list[str] = [
        "# CodePilot secrets override file",
        f"# Keep this file out of version control. Default name: {config_mod.SECRETS_FILENAME}",
        "",
    ]

    if providers:
        lines.append("[providers]")
        lines.append("")
        for name, values in providers.items():
            api_key = str(values.get("api_key", "") or "").strip()
            if not api_key:
                continue
            lines.append(f"[providers.{name}]")
            lines.append(f"api_key = {_toml_value(api_key)}")
            lines.append("")

    app_secret = str(feishu_bot.get("app_secret", "") or "").strip()
    if app_secret:
        lines.append("[feishu_bot]")
        lines.append(f"app_secret = {_toml_value(app_secret)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _resolve_config_target(path: Path | None, *, use_global: bool = False) -> Path:
    if use_global:
        return config_mod.resolve_global_config_path()

    if path is None:
        found = config_mod.find_config()
        if found is not None:
            return found.resolve()
        return (Path.cwd() / config_mod.CONFIG_FILENAME).resolve()
    target = path.expanduser()
    if target.is_dir():
        return (target / config_mod.CONFIG_FILENAME).resolve()
    return target.resolve()


def _raw_config_errors(data: dict[str, Any]) -> list[str]:
    """Validate raw values that canonicalization would otherwise normalize away."""
    errors: list[str] = []
    automation = data.get("automation")
    if automation is None:
        return errors
    if not isinstance(automation, dict):
        return ["[automation] 必须是 TOML table。"]

    if "task_workspace" in automation:
        raw_workspace = automation.get("task_workspace")
        workspace = str(raw_workspace).strip().lower() if isinstance(raw_workspace, str) else ""
        if workspace not in {"direct", "branch", "worktree"}:
            errors.append(f"automation.task_workspace 值无效: {raw_workspace}")
    if "preflight_dirty_worktree" in automation:
        try:
            config_mod.normalize_preflight_dirty_worktree(automation.get("preflight_dirty_worktree"))
        except config_mod.ConfigError as exc:
            errors.append(str(exc))
    if "agent_language" in automation:
        try:
            config_mod.normalize_agent_language(automation.get("agent_language"))
        except config_mod.ConfigError as exc:
            errors.append(str(exc))
    return errors


@click.group("config")
def config_group() -> None:
    """维护 AGENTS.toml 配置文件。"""


@config_group.command("sync")
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.option("--global", "global_mode", is_flag=True, help="同步全局配置（~/.codepilot/AGENTS.toml）")
@click.option("--dry-run", is_flag=True, help="只输出同步后的内容，不写文件")
def sync(path: Path | None, global_mode: bool, dry_run: bool) -> None:
    """同步 AGENTS.toml：补默认项、移除未知项、保留已有有效值。"""
    if global_mode and path is not None:
        raise click.ClickException("--global 与 PATH 不能同时使用。")

    config_path = _resolve_config_target(path, use_global=global_mode)
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            with open(config_path, "rb") as handle:
                import tomllib

                data = tomllib.load(handle)
        except Exception as exc:
            raise click.ClickException(f"AGENTS.toml 解析失败: {exc}") from exc

    project_name = config_path.parent.name
    canonical = _canonical_config(data, project_name=project_name)
    sync_secrets = _extract_sync_secrets(data)
    existing_secrets = _canonical_secrets(_load_toml_dict(config_path.parent / config_mod.SECRETS_FILENAME))
    existing_feishu_secret = ""
    existing_feishu = existing_secrets.get("feishu_bot")
    if isinstance(existing_feishu, dict):
        existing_feishu_secret = str(existing_feishu.get("app_secret", "") or "").strip()
    content = render_agents_toml(canonical)
    write_secrets = bool(sync_secrets) and not existing_feishu_secret
    secrets_content = ""
    if write_secrets:
        merged_secrets = _merge_secret_dicts(existing_secrets, sync_secrets)
        secrets_content = render_secrets_toml(merged_secrets)

    if dry_run:
        click.echo(content, nl=False)
        return

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
    if write_secrets and secrets_content:
        secrets_path = config_path.parent / config_mod.SECRETS_FILENAME
        secrets_path.write_text(secrets_content, encoding="utf-8")
        if not global_mode:
            ensure_gitignore_entry(config_path.parent, config_mod.SECRETS_FILENAME)
    click.echo(f"已同步配置: {config_path}")
    if write_secrets:
        click.echo(f"已迁移飞书 App Secret 到: {config_path.parent / config_mod.SECRETS_FILENAME}")


@config_group.command("init")
@click.option("--global", "global_mode", is_flag=True, help="初始化全局配置（~/.codepilot/AGENTS.toml）")
@click.option("--path", "path", type=click.Path(path_type=Path), help="指定配置目录或文件路径")
@click.option("--non-interactive", is_flag=True, help="非交互模式，使用默认值")
def init_config(global_mode: bool, path: Path | None, non_interactive: bool) -> None:
    """交互式初始化 AGENTS.toml 配置文件。"""
    import shutil
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    # 确定配置路径
    if global_mode:
        config_path = config_mod.resolve_global_config_path()
    elif path is not None:
        target = Path(path).expanduser()
        config_path = target if target.is_file() else (target / config_mod.CONFIG_FILENAME)
    else:
        found = config_mod.find_config()
        if found is not None:
            config_path = found.resolve()
        else:
            config_path = (Path.cwd() / config_mod.CONFIG_FILENAME).resolve()

    # 如果配置文件已存在，询问是否覆盖
    if config_path.exists():
        if non_interactive:
            raise click.ClickException(f"配置文件已存在: {config_path}。如需同步现有配置，请运行 codepilot config sync。")
        overwrite = click.confirm(f"配置文件已存在: {config_path}\n是否覆盖？", default=False)
        if not overwrite:
            click.echo("已取消。")
            return

    console.print(Panel.fit("[bold blue]CodePilot 配置向导[/bold blue]", border_style="blue"))

    # 初始化配置数据
    data: dict[str, Any] = {}
    project_name = config_path.parent.name

    # 1. 项目配置
    console.print("\n[bold]1. 项目配置[/bold]")
    if not non_interactive:
        project_name = click.prompt("项目名称", default=project_name, show_default=True)
        base_branch = click.prompt("Git 主分支", default="main", show_default=True)
        default_mode = click.prompt("默认任务智能体", default="codex", show_default=True)
    else:
        base_branch = "main"
        default_mode = "codex"

    data["project"] = {
        "name": project_name,
        "base_branch": base_branch,
        "default_mode": default_mode,
    }

    # 2. 检测 CLI 工具
    console.print("\n[bold]2. 检测 CLI 工具[/bold]")
    cli_tools = ["claude", "codex", "opencode"]
    detected_tools: dict[str, str] = {}

    for tool in cli_tools:
        tool_path = shutil.which(tool)
        if tool_path:
            detected_tools[tool] = tool_path
            console.print(f"  ✓ {tool}: [green]{tool_path}[/green]")
        else:
            console.print(f"  ✗ {tool}: [dim]未找到[/dim]")

    if not non_interactive and detected_tools:
        console.print("\n配置 CLI 命令映射？（回车跳过则使用默认）")
        commands: dict[str, str] = {}
        for tool in detected_tools:
            cmd = click.prompt(f"  {tool} 命令", default=tool, show_default=True)
            commands[tool] = cmd
        data["agents"] = {"commands": commands}
    else:
        data["agents"] = {"commands": {}}

    # 3. 配置 AI Provider
    console.print("\n[bold]3. AI Provider 配置[/bold]")

    providers: dict[str, Any] = {}
    provider_choices = ["deepseek", "openai", "claude", "qwen", "hunyuan", "ollama", "custom"]

    if not non_interactive:
        while True:
            console.print("\n可选 Provider:")
            for i, p in enumerate(provider_choices, 1):
                console.print(f"  {i}. {p}")
            console.print("  0. 完成配置")

            choice = click.prompt("选择要配置的 Provider（输入编号）", default="0", show_default=False)

            if choice == "0":
                break

            try:
                idx = int(choice) - 1
                if idx < 0 or idx >= len(provider_choices):
                    console.print("[red]无效的选择[/red]")
                    continue
                provider_name = provider_choices[idx]
                provider_config = _interactive_provider_config(provider_name, console)
                if provider_config:
                    providers[provider_name] = provider_config
            except ValueError:
                console.print("[red]请输入数字[/red]")
    else:
        # 非交互模式：默认启用 deepseek
        providers["deepseek"] = {
            "enabled": True,
            "model": "",
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "max_tokens": 8192,
            "temperature": 0.7,
            "auto_model_selection": True,
            "simple_model": "deepseek-v4-flash",
            "complex_model": "deepseek-v4-pro",
        }

    data["providers"] = providers

    # 4. 自动化配置
    console.print("\n[bold]4. 自动化配置[/bold]")
    if not non_interactive:
        task_agent = click.prompt("任务执行智能体", default="dual", show_default=True)
        task_workspace = click.prompt("任务工作空间 (direct/branch/worktree)", default="branch", show_default=True)
        auto_execute = click.confirm("是否自动执行任务？", default=True)
    else:
        task_agent = "dual"
        task_workspace = "branch"
        auto_execute = True

    data["automation"] = {
        "task_agent": task_agent,
        "task_workspace": task_workspace,
        "agent_language": "en",
        "auto_execute": auto_execute,
        "auto_commit": True,
        "max_tasks": 5,
        "max_retries": 3,
        "per_task_branch": True,
        "two_stage_planning": True,
        "clarify_vague_requirements": True,
    }

    # 5. 飞书机器人（可选）
    if not non_interactive:
        console.print("\n[bold]5. 飞书机器人配置（可选）[/bold]")
        enable_feishu = click.confirm("是否配置飞书机器人？", default=False)
        if enable_feishu:
            app_id = click.prompt("飞书 App ID", default="", show_default=False)
            data["feishu_bot"] = {
                "enabled": True,
                "app_id": app_id,
                "node_command": "node",
                "default_project": project_name,
                "command_prefix": "",
            }
        else:
            data["feishu_bot"] = {"enabled": False}
    else:
        data["feishu_bot"] = {"enabled": False}

    # 生成规范化的配置
    canonical = _canonical_config(data, project_name=project_name)

    # 如果有 API Key，写入 secrets 文件
    secrets_data: dict[str, Any] = {"providers": {}, "feishu_bot": {}}
    has_secrets = False

    for name, cfg in providers.items():
        api_key = cfg.get("api_key", "")
        if api_key:
            if "providers" not in secrets_data:
                secrets_data["providers"] = {}
            secrets_data["providers"][name] = {"api_key": api_key}
            has_secrets = True
            # 从主配置中移除 API Key
            canonical["providers"][name]["api_key"] = ""

    # 写入配置文件
    config_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_agents_toml(canonical)
    config_path.write_text(content, encoding="utf-8")

    console.print(f"\n[green]✓[/green] 配置文件已生成: {config_path}")

    if has_secrets:
        secrets_path = config_path.parent / config_mod.SECRETS_FILENAME
        secrets_content = render_secrets_toml(_canonical_secrets(secrets_data))
        secrets_path.write_text(secrets_content, encoding="utf-8")
        if not global_mode:
            ensure_gitignore_entry(config_path.parent, config_mod.SECRETS_FILENAME)
        console.print(f"[green]✓[/green] Secrets 已写入: {secrets_path}")

    console.print(Panel.fit("[bold green]配置完成！[/bold green]", border_style="green"))
    console.print("\n下一步：")
    console.print("  1. 编辑配置文件：", config_path)
    console.print("  2. 验证配置：codepilot config validate")
    console.print("  3. 检查环境：codepilot doctor")


def _interactive_provider_config(provider_name: str, console: Any) -> dict[str, Any] | None:
    """交互式配置单个 Provider。"""
    console.print(f"\n配置 [bold]{provider_name}[/bold] Provider:")

    enabled = click.confirm("  是否启用？", default=(provider_name == "deepseek"))
    if not enabled:
        return None

    config = _get_provider_example(provider_name)
    config["enabled"] = True

    api_key = click.prompt("  API Key（留空使用环境变量）", default="", show_default=False)
    if api_key:
        config["api_key"] = api_key

    model = click.prompt("  模型名称", default=config.get("model", ""), show_default=bool(config.get("model")))
    if model:
        config["model"] = model

    base_url = click.prompt("  自定义接口地址（留空使用默认）", default=config.get("base_url", ""), show_default=False)
    if base_url:
        config["base_url"] = base_url

    return config


def _get_provider_example(provider_name: str) -> dict[str, Any]:
    """获取 Provider 示例配置。"""
    if provider_name in PROVIDER_EXAMPLES:
        return PROVIDER_EXAMPLES[provider_name].copy()
    return {
        "enabled": True,
        "model": "",
        "base_url": "",
        "api_key": "",
        "max_tokens": 4096,
        "temperature": 0.7,
    }


@config_group.command("validate")
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.option("--global", "global_mode", is_flag=True, help="验证全局配置")
@click.option("--fix", is_flag=True, help="自动修复可修复的问题")
def validate_config(path: Path | None, global_mode: bool, fix: bool) -> None:
    """验证 AGENTS.toml 配置文件的完整性和正确性。"""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    # 确定配置路径
    config_path = _resolve_config_target(path, use_global=global_mode)
    if not config_path.exists():
        console.print(f"[red]✗[/red] 配置文件不存在: {config_path}")
        console.print("\n运行 [bold]codepilot config init[/bold] 来创建配置文件。")
        raise SystemExit(1)

    console.print(Panel.fit(f"[bold]验证配置: {config_path}[/bold]", border_style="blue"))

    errors: list[str] = []
    warnings: list[str] = []
    fixes: list[str] = []

    # 1. 验证 TOML 格式
    console.print("\n[bold]1. 检查 TOML 格式...[/bold]")
    try:
        data = _load_toml_dict(config_path)
        console.print("[green]✓[/green] TOML 格式正确")
    except Exception as exc:
        console.print(f"[red]✗[/red] TOML 格式错误: {exc}")
        raise SystemExit(1) from exc

    # 2. 验证配置完整性
    console.print("\n[bold]2. 检查配置完整性...[/bold]")
    errors.extend(_raw_config_errors(data))
    try:
        canonical = _canonical_config(data, project_name=config_path.parent.name)
    except config_mod.ConfigError as exc:
        message = str(exc)
        if message and message not in errors:
            errors.append(message)
        safe_data = dict(data)
        safe_automation = dict(safe_data.get("automation") or {})
        safe_automation.pop("agent_language", None)
        safe_data["automation"] = safe_automation
        canonical = _canonical_config(safe_data, project_name=config_path.parent.name)

    # 检查必需字段
    project = canonical.get("project", {})
    if not project.get("name"):
        errors.append("project.name 未设置")
    if not project.get("base_branch"):
        warnings.append("project.base_branch 未设置，默认使用 'main'")

    # 3. 验证 Provider 配置
    console.print("\n[bold]3. 检查 AI Provider...[/bold]")
    providers = canonical.get("providers", {})
    if not providers:
        warnings.append("未配置任何 Provider，AI 功能将不可用")

    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            errors.append(f"Provider {name} 配置格式错误")
            continue

        if cfg.get("enabled"):
            api_key = cfg.get("api_key", "")
            if not api_key:
                # 检查环境变量
                env_var = f"{name.upper()}_API_KEY"
                if env_var not in __import__("os").environ:
                    warnings.append(f"Provider {name} 未设置 api_key，且环境变量 {env_var} 未设置")

            model = cfg.get("model", "")
            if not model and not cfg.get("auto_model_selection"):
                warnings.append(f"Provider {name} 未设置 model")

    # 4. 验证 CLI 工具可用性
    console.print("\n[bold]4. 检查 CLI 工具...[/bold]")
    import shutil

    agents = canonical.get("agents", {})
    commands = agents.get("commands", {})
    if isinstance(commands, dict):
        for family, cmd in commands.items():
            cmd_str = str(cmd)
            if "/" in cmd_str or "\\" in cmd_str:
                # 路径
                if not Path(cmd_str).exists():
                    warnings.append(f"CLI 工具路径不存在: {cmd_str}")
            else:
                # 命令
                if not shutil.which(cmd_str):
                    warnings.append(f"CLI 工具未找到: {cmd_str}")

    # 5. 验证自动化配置
    console.print("\n[bold]5. 检查自动化配置...[/bold]")
    automation = canonical.get("automation", {})
    task_workspace = automation.get("task_workspace", "branch")
    if task_workspace not in {"direct", "branch", "worktree"}:
        errors.append(f"automation.task_workspace 值无效: {task_workspace}")
        if fix:
            automation["task_workspace"] = "branch"
            fixes.append("automation.task_workspace 已修复为 'branch'")

    # 输出结果
    console.print("\n[bold]验证结果:[/bold]")

    if warnings:
        console.print(f"\n[yellow]警告 ({len(warnings)}):[/yellow]")
        for i, w in enumerate(warnings, 1):
            console.print(f"  {i}. {w}")

    if errors:
        console.print(f"\n[red]错误 ({len(errors)}):[/red]")
        for i, e in enumerate(errors, 1):
            console.print(f"  {i}. {e}")

    if not errors and not warnings:
        console.print("\n[green]✓[/green] 配置验证通过，未发现问题。")

    # 自动修复
    if fix and fixes:
        console.print(f"\n[bold]自动修复 ({len(fixes)}):[/bold]")
        for fix_msg in fixes:
            console.print(f"  • {fix_msg}")

        # 重新渲染配置
        content = render_agents_toml(canonical)
        config_path.write_text(content, encoding="utf-8")
        console.print(f"\n[green]✓[/green] 配置文件已更新: {config_path}")

    # 退出码
    if errors:
        raise SystemExit(1)
    elif warnings:
        console.print("\n[yellow]配置验证完成，发现警告。[/yellow]")
        raise SystemExit(0)
    else:
        console.print("\n[green]配置验证完成，未发现问题。[/green]")
        raise SystemExit(0)
