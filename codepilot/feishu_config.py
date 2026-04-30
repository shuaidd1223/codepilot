"""Feishu bot configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codepilot.core.config import load_config


@dataclass
class FeishuBotConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    node_command: str = "node"
    default_project: str = ""
    command_prefix: str = ""


def load_feishu_bot_config(config_path: Path | None = None) -> FeishuBotConfig:
    cfg = load_config(config_path)
    if cfg is None:
        return FeishuBotConfig()
    section = cfg.feishu_bot if isinstance(cfg.feishu_bot, dict) else {}
    return FeishuBotConfig(
        enabled=bool(cfg.feishu_bot_enabled or section.get("enabled", False)),
        app_id=str(cfg.feishu_app_id or section.get("app_id", "") or ""),
        app_secret=str(cfg.feishu_app_secret or section.get("app_secret", "") or ""),
        node_command=str(cfg.feishu_node_command or section.get("node_command", "node") or "node"),
        default_project=str(cfg.feishu_default_project or section.get("default_project", "") or ""),
        command_prefix=str(cfg.feishu_command_prefix or section.get("command_prefix", "") or "").strip(),
    )


def validate_feishu_bot_config(config_path: Path | None = None) -> list[str]:
    cfg = load_feishu_bot_config(config_path)
    problems: list[str] = []
    if not cfg.enabled:
        problems.append("[feishu_bot].enabled = true 未开启。")
    if not cfg.app_id:
        problems.append("[feishu_bot].app_id 为空。")
    if not cfg.app_secret:
        problems.append("[feishu_bot].app_secret 为空。")
    if not cfg.node_command:
        problems.append("[feishu_bot].node_command 为空。")
    return problems
