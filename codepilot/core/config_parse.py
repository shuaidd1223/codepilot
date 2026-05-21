"""Shared AGENTS.toml parsing helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Optional

from codepilot.errors import CodePilotError


class ConfigError(CodePilotError, ValueError):
    """AGENTS.toml 配置解析错误。同时兼容 ``except ValueError`` 和 ``except CodePilotError``。"""


DEFAULT_AGENT_COMMANDS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "cp-opencode",
}
DEFAULT_FALLBACK_CLI_ORDER: list[str] = ["claude", "codex", "opencode"]
PREFLIGHT_DIRTY_WORKTREE_POLICIES: tuple[str, ...] = ("stop", "commit", "stash")
SUPPORTED_AGENT_LANGUAGES: tuple[str, ...] = ("en", "zh-CN")
LEGACY_AGENT_COMMAND_KEYS: tuple[str, ...] = ("codex_cmd", "claude_cmd")
INTERVAL_PATTERN = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)
INTERVAL_MULTIPLIERS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}
AGENT_COMMANDS_MIGRATION_HINT = (
    "[agents] codex_cmd / claude_cmd 已被移除，改用 [agents.commands] 映射。"
    "示例：\n\n"
    "[agents.commands]\n"
    'claude = "claude"\n'
    'codex = "codex"\n'
    'opencode = "opencode"\n\n'
    "[automation]\n"
    'fallback_cli_order = ["claude", "codex", "opencode"]\n'
)


def _check_legacy_agent_command_keys(agents_section: Mapping[str, object]) -> None:
    """Raise ConfigError when the loaded config still uses the old scalar keys."""
    legacy_present = [k for k in LEGACY_AGENT_COMMAND_KEYS if k in agents_section]
    if not legacy_present:
        return
    raise ConfigError(
        "AGENTS.toml 含已废弃字段：" + ", ".join(legacy_present) + "。\n"
        + AGENT_COMMANDS_MIGRATION_HINT
    )


def _parse_agent_commands(raw: object) -> dict[str, str]:
    """Parse the `[agents.commands]` table into a dict, applying defaults."""
    merged = dict(DEFAULT_AGENT_COMMANDS)
    if raw is None:
        return merged
    if not isinstance(raw, Mapping):
        raise ConfigError(
            "[agents.commands] 必须是表/字典；请使用 `[agents.commands]` 子表格式。"
        )
    for family, value in raw.items():
        family_name = str(family).strip()
        if not family_name:
            continue
        cmd = str(value or "").strip()
        if cmd:
            merged[family_name] = cmd
    return merged


def _parse_fallback_cli_order(raw: object) -> list[str]:
    """Parse `[automation].fallback_cli_order` with sane defaults."""
    if raw is None:
        return list(DEFAULT_FALLBACK_CLI_ORDER)
    if isinstance(raw, str):
        # Single string is allowed as a one-element order.
        return [raw.strip()] if raw.strip() else list(DEFAULT_FALLBACK_CLI_ORDER)
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(
            "[automation].fallback_cli_order 必须是字符串列表，例如 "
            '["claude", "codex", "opencode"]。'
        )
    cleaned = [str(item).strip() for item in raw if str(item or "").strip()]
    return cleaned or list(DEFAULT_FALLBACK_CLI_ORDER)


def _parse_optional_string_list(raw: object, path: str) -> tuple[str, ...] | None:
    """Parse optional string-or-list config values without applying defaults."""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return (text,) if text else tuple()
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(f"{path} 必须是字符串或字符串列表。")
    return tuple(str(item).strip() for item in raw if str(item or "").strip())


def normalize_agent_language(raw: object = None) -> str:
    """Normalize the project-level agent prompt/output language."""
    text = str(raw or "").strip()
    if not text:
        return "en"
    lowered = text.lower().replace("_", "-")
    if lowered in {"en", "en-us", "english"}:
        return "en"
    if lowered in {"zh", "zh-cn", "zh-hans", "chinese", "cn"} or text in {"中文", "简体中文"}:
        return "zh-CN"
    raise ConfigError(
        "automation.agent_language 只支持 en 或 zh-CN；"
        "可用别名包括 en-US/english、zh/zh-CN/中文。"
    )


def normalize_preflight_dirty_worktree(raw: object = None) -> str:
    """Normalize how builtin preflight handles an already-dirty worktree."""
    text = str(raw or "").strip()
    if not text:
        return "stop"
    lowered = text.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "stop": "stop",
        "block": "stop",
        "halt": "stop",
        "fail": "stop",
        "停止执行": "stop",
        "commit": "commit",
        "auto_commit": "commit",
        "analyze_commit": "commit",
        "analyze_then_commit": "commit",
        "分析后提交": "commit",
        "stash": "stash",
        "stash_and_log": "stash",
        "stash_then_log": "stash",
        "暂存": "stash",
        "暂存并记录": "stash",
        "暂存并写文档": "stash",
        "暂存并写日志": "stash",
        "暂存并写日志记录": "stash",
        "暂存并写文档/日志记录": "stash",
    }
    normalized = aliases.get(lowered) or aliases.get(text)
    if normalized:
        return normalized
    raise ConfigError(
        "automation.preflight_dirty_worktree 只支持 stop、commit、stash；"
        "分别表示停止执行、分析后提交、stash 并记录。"
    )


def _required_config_text(raw: object, path: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ConfigError(f"{path} 必须配置非空字符串。")
    return text


def _optional_config_text(raw: object) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _parse_interval_seconds(raw: object, path: str) -> int:
    text = _required_config_text(raw, path)
    match = INTERVAL_PATTERN.match(text)
    if not match:
        raise ConfigError(
            f"{path} 必须使用 10m、1h、1d 这类 interval 格式。"
        )
    value = int(match.group(1))
    if value <= 0:
        raise ConfigError(f"{path} 必须大于 0。")
    unit = match.group(2).lower()
    return value * INTERVAL_MULTIPLIERS[unit]


def _parse_optional_cost(raw: object, path: str) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} 必须是非负数字。") from exc
    if value < 0:
        raise ConfigError(f"{path} 必须是非负数字。")
    return value


def _parse_named_config_table(raw: object, path: str) -> dict[str, Mapping[str, object]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"[{path}] 必须是 TOML table。")

    parsed: dict[str, Mapping[str, object]] = {}
    for raw_name, raw_cfg in raw.items():
        name = str(raw_name).strip()
        if not name:
            raise ConfigError(f"[{path}] 包含空名称。")
        if not isinstance(raw_cfg, Mapping):
            raise ConfigError(f"[{path}.{name}] 必须是 TOML table。")
        parsed[name] = raw_cfg
    return parsed


def _parse_opencode_permission(raw_opencode: object) -> dict[str, Any]:
    """Parse the project-level `[opencode.permission]` table.

    Other `[opencode]` subtables are intentionally ignored here because
    CodePilot owns the tool-level OpenCode profile, branding, agents and TUI.
    """
    if raw_opencode is None:
        return {}
    if not isinstance(raw_opencode, Mapping):
        raise ConfigError("[opencode] 必须是 TOML table。")
    raw_permission = raw_opencode.get("permission")
    if raw_permission is None:
        return {}
    if not isinstance(raw_permission, Mapping):
        raise ConfigError("[opencode.permission] 必须是 TOML table。")
    return deepcopy(dict(raw_permission))


def _normalize_optional_agent_name(value: object) -> Optional[str]:
    """Normalize an optional agent name from ``[agents]`` config values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    from codepilot.ai_support.service import normalize_agent_name

    normalized = normalize_agent_name(text).strip()
    return normalized or None
