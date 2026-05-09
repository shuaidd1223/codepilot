"""AGENTS.toml maintenance commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from codepilot.core import config as config_mod


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
    try:
        with open(path, "rb") as handle:
            import tomllib

            data = tomllib.load(handle)
    except Exception:
        return {}
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
    classifier = data.get("classifier") if isinstance(data.get("classifier"), dict) else {}
    inspect = data.get("inspect") if isinstance(data.get("inspect"), dict) else {}
    notifications = data.get("notifications") if isinstance(data.get("notifications"), dict) else {}
    feishu_bot = data.get("feishu_bot") if isinstance(data.get("feishu_bot"), dict) else {}

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
        },
        "classifier": {
            "provider": _string(classifier.get("provider"), ""),
            "model": _string(classifier.get("model"), ""),
            "enabled": _bool(classifier.get("enabled"), True),
            "timeout": _int(classifier.get("timeout"), 30, min_value=1),
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

    classifier_provider = _optional_string(classifier.get("provider"))
    if classifier_provider and classifier_provider not in canonical["providers"]:
        example = PROVIDER_EXAMPLES.get(classifier_provider, {})
        canonical["providers"][classifier_provider] = _supported_provider(example) or _supported_provider({}) or {}

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
    "dispatch": [
        "外部 dispatch 执行器配置。builtin 执行器也会读取 stale_minutes 等通用超时语义。",
    ],
    "automation": [
        "自动规划与执行配置。",
        "task_workspace: direct=主工作区直接改；branch=主工作区临时分支；worktree=独立临时 worktree。",
    ],
    "classifier": [
        "意图分类/问答/澄清用的模型。provider 留空时走本地 CLI 兜底。",
        "model 留空时使用 [providers.<provider>].model 或内置 provider 默认模型。",
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
    ("automation", "two_stage_planning"): ["规划前先侦察代码，再拆任务。"],
    ("automation", "clarify_vague_requirements"): ["需求模糊时先反问澄清。"],
    ("automation", "clarify_max_turns"): ["最多澄清轮数，达到后按当前信息规划。"],
    ("automation", "max_review_rounds"): ["Builder/Reviewer 闭环最大轮数；1 等于关闭闭环。"],
    ("automation", "agent_silence_timeout_seconds"): ["CLI 连续无输出多少秒后终止；0 表示关闭保护。"],
    ("automation", "fallback_cli_order"): ["文本模式 CLI 兜底顺序；前面项不可用时按顺序退到下一个。"],
    ("classifier", "provider"): ["API provider key，例如 openai-gpt4o、claude-sonnet、deepseek；留空走本地 CLI。"],
    ("classifier", "model"): ["覆盖分类/问答/澄清模型；留空使用 provider.model。"],
    ("classifier", "enabled"): ["false 时跳过意图分类，输入默认当作需求处理。"],
    ("classifier", "timeout"): ["分类/澄清 API 或 CLI 调用超时时间秒数。"],
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
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")
    for sub_key, sub_values in nested:
        _emit_subtable(lines, f"{title}.{sub_key}", sub_values)


def _emit_subtable(lines: list[str], title: str, values: dict[str, Any]) -> None:
    """Emit a nested TOML sub-table, e.g. [agents.commands]."""
    _append_comments(lines, SECTION_COMMENTS.get(title, []))
    lines.append(f"[{title}]")
    for key, value in values.items():
        _append_comments(lines, KEY_COMMENTS.get((title, key), []))
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")


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
    for section in ("project", "shell", "agents", "dispatch", "automation", "classifier", "inspect"):
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
    click.echo(f"已同步配置: {config_path}")
    if write_secrets:
        click.echo(f"已迁移飞书 App Secret 到: {config_path.parent / config_mod.SECRETS_FILENAME}")
