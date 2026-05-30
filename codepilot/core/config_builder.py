"""Build typed config objects from raw AGENTS.toml dictionaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from codepilot.core.config_parse import (
    ConfigError,
    _check_legacy_agent_command_keys,
    _normalize_optional_agent_name,
    _optional_config_text,
    _parse_agent_commands,
    _parse_fallback_cli_order,
    _parse_interval_seconds,
    _parse_named_config_table,
    _parse_opencode_permission,
    _parse_optional_cost,
    _parse_optional_string_list,
    _parse_positive_int,
    _required_config_text,
    normalize_agent_language,
    normalize_preflight_dirty_worktree,
)

if TYPE_CHECKING:
    from codepilot.core.config import AgentsConfig, EventAgentConfig, ScheduledAgentConfig


def _parse_scheduled_agents(raw: object) -> dict[str, "ScheduledAgentConfig"]:
    from codepilot.core.config import ScheduledAgentConfig

    parsed: dict[str, ScheduledAgentConfig] = {}
    for name, cfg in _parse_named_config_table(raw, "automation.scheduled_agents").items():
        path = f"automation.scheduled_agents.{name}"
        agent = _required_config_text(cfg.get("agent"), f"{path}.agent")
        prompt = _required_config_text(cfg.get("prompt"), f"{path}.prompt")
        interval = _optional_config_text(cfg.get("interval"))
        schedule = _optional_config_text(cfg.get("schedule"))
        if not interval and not schedule:
            raise ConfigError(f"{path} 必须配置 interval 或 schedule。")
        parsed[name] = ScheduledAgentConfig(
            enabled=bool(cfg.get("enabled", True)),
            agent=agent,
            prompt=prompt,
            interval=interval,
            interval_seconds=(
                _parse_interval_seconds(interval, f"{path}.interval")
                if interval is not None
                else None
            ),
            schedule=schedule,
            max_cost_usd=_parse_optional_cost(cfg.get("max_cost_usd"), f"{path}.max_cost_usd"),
            max_daily_cost_usd=_parse_optional_cost(
                cfg.get("max_daily_cost_usd"),
                f"{path}.max_daily_cost_usd",
            ),
        )
    return parsed


def _parse_event_agents(raw: object) -> dict[str, "EventAgentConfig"]:
    from codepilot.core.config import EventAgentConfig

    parsed: dict[str, EventAgentConfig] = {}
    for name, cfg in _parse_named_config_table(raw, "automation.event_agents").items():
        path = f"automation.event_agents.{name}"
        parsed[name] = EventAgentConfig(
            enabled=bool(cfg.get("enabled", True)),
            trigger=_required_config_text(cfg.get("trigger"), f"{path}.trigger"),
            agent=_required_config_text(cfg.get("agent"), f"{path}.agent"),
            prompt=_required_config_text(cfg.get("prompt"), f"{path}.prompt"),
            max_cost_usd=_parse_optional_cost(cfg.get("max_cost_usd"), f"{path}.max_cost_usd"),
            max_daily_cost_usd=_parse_optional_cost(
                cfg.get("max_daily_cost_usd"),
                f"{path}.max_daily_cost_usd",
            ),
        )
    return parsed


def build_agents_config_from_dict(
    config_cls: type["AgentsConfig"],
    data: dict[str, Any],
    config_file_path: Optional[str] = None,
) -> "AgentsConfig":
    """Build an ``AgentsConfig`` from raw TOML data."""
    from codepilot.core.config import (
        AutomationConfig,
        DispatchConfig,
        InspectConfig,
        ProjectConfig,
        ProviderAPIConfig,
        ShellConfig,
    )
    proj = data.get("project", {})
    agents = data.get("agents", {}) or {}
    _check_legacy_agent_command_keys(agents)
    dispatch = data.get("dispatch", {})
    automation = data.get("automation", {})
    inspect = data.get("inspect", {})
    notifications = data.get("notifications", {})
    feishu_bot = data.get("feishu_bot", {})
    shell = data.get("shell", {})
    providers = data.get("providers", {})
    opencode = data.get("opencode", {})
    commands_map = _parse_agent_commands(agents.get("commands"))
    fallback_cli_order = _parse_fallback_cli_order(automation.get("fallback_cli_order"))
    agent_language = normalize_agent_language(automation.get("agent_language"))
    scheduled_agents = _parse_scheduled_agents(automation.get("scheduled_agents"))
    event_agents = _parse_event_agents(automation.get("event_agents"))
    opencode_permission = _parse_opencode_permission(opencode)

    # 解析 providers
    providers_config = {}
    for name, cfg in providers.items():
        if isinstance(cfg, dict):
            providers_config[name] = ProviderAPIConfig(
                enabled=cfg.get("enabled", True),
                api_key=cfg.get("api_key", ""),
                model=cfg.get("model", ""),
                base_url=cfg.get("base_url", ""),
                max_tokens=cfg.get("max_tokens", 4096),
                temperature=cfg.get("temperature", 0.7),
                auto_model_selection=(
                    cfg.get("auto_model_selection")
                    if isinstance(cfg.get("auto_model_selection"), bool)
                    else None
                ),
                simple_model=str(cfg.get("simple_model", "") or ""),
                complex_model=str(cfg.get("complex_model", "") or ""),
                thinking=str(cfg.get("thinking", "") or ""),
                reasoning_effort=str(cfg.get("reasoning_effort", "") or ""),
            )
        else:
            providers_config[name] = ProviderAPIConfig(enabled=bool(cfg))

    return config_cls(
        project=ProjectConfig(
            name=proj.get("name", ""),
            base_branch=proj.get("base_branch", "dev"),
            default_mode=proj.get("default_mode", "dual"),
            worktree_base=proj.get("worktree_base"),
        ),
        shell=ShellConfig(
            preferred=shell.get("preferred", "auto"),
            powershell_path=shell.get("powershell_path"),
            bash_path=shell.get("bash_path"),
        ),
        dispatch=DispatchConfig(
            dispatch_path=dispatch.get("dispatch_path", ""),
            interval_seconds=dispatch.get("interval_seconds", 600),
            stale_minutes=dispatch.get("stale_minutes", 30),
        ),
        automation=AutomationConfig(
            planner=automation.get("planner", "codex"),
            default_agent=automation.get("default_agent", ""),
            task_agent=automation.get("task_agent", "dual"),
            executor=automation.get("executor", "builtin"),
            auto_execute=automation.get("auto_execute", True),
            confirm_before_execute=automation.get("confirm_before_execute", False),
            auto_commit=automation.get("auto_commit", True),
            max_tasks=automation.get("max_tasks", 5),
            max_retries=automation.get("max_retries", 3),
            per_task_branch=automation.get("per_task_branch", True),
            task_workspace=automation.get("task_workspace", "branch"),
            worktree_context_patterns=_parse_optional_string_list(
                automation.get("worktree_context_patterns"),
                "automation.worktree_context_patterns",
            ),
            worktree_context_link_patterns=_parse_optional_string_list(
                automation.get("worktree_context_link_patterns"),
                "automation.worktree_context_link_patterns",
            ),
            preflight_dirty_worktree=normalize_preflight_dirty_worktree(
                automation.get("preflight_dirty_worktree")
            ),
            two_stage_planning=automation.get("two_stage_planning", True),
            max_review_rounds=automation.get("max_review_rounds", 2),
            agent_silence_timeout_seconds=automation.get("agent_silence_timeout_seconds", 0),
            workflow_auto_create_inspect_tasks=bool(
                automation.get("workflow_auto_create_inspect_tasks", False)
            ),
            workflow_auto_import_plan_tasks=bool(
                automation.get("workflow_auto_import_plan_tasks", False)
            ),
            workflow_auto_max_steps=_parse_positive_int(
                automation.get("workflow_auto_max_steps"),
                "automation.workflow_auto_max_steps",
                default=1,
            ),
            workflow_auto_failure_threshold=_parse_positive_int(
                automation.get("workflow_auto_failure_threshold"),
                "automation.workflow_auto_failure_threshold",
                default=1,
            ),
            fallback_cli_order=fallback_cli_order,
            agent_language=agent_language,
            scheduled_agents=scheduled_agents,
            event_agents=event_agents,
        ),
        inspect=InspectConfig(
            enabled=inspect.get("enabled", False),
            interval_seconds=inspect.get("interval_seconds", 1800),
            max_new_tasks_per_round=inspect.get("max_new_tasks_per_round", 3),
            signals=tuple(inspect.get("signals", ["git_log", "failed_tasks", "todos"])),
            auto_execute=inspect.get("auto_execute", False),
            planner=_normalize_optional_agent_name(inspect.get("planner")),
            priority=inspect.get("priority", "P3"),
        ),
        notifications=notifications,
        feishu_bot=feishu_bot,
        providers=providers_config,
        opencode_permission=opencode_permission,
        # 兼容字段
        project_name=proj.get("name", ""),
        base_branch=proj.get("base_branch", "dev"),
        default_mode=proj.get("default_mode", "dual"),
        worktree_base=proj.get("worktree_base"),
        planner=_normalize_optional_agent_name(agents.get("planner")),
        builder=_normalize_optional_agent_name(agents.get("builder")),
        reviewer=_normalize_optional_agent_name(agents.get("reviewer")),
        commands=commands_map,
        interval_seconds=dispatch.get("interval_seconds", 600),
        stale_minutes=dispatch.get("stale_minutes", 30),
        webhook_url=notifications.get("webhook_url", ""),
        webhook_provider=str(notifications.get("provider", "auto") or "auto"),
        webhook_secret=notifications.get("webhook_secret", ""),
        notifications_enabled=notifications.get("enabled", False),
        feishu_bot_enabled=bool(feishu_bot.get("enabled", False)),
        feishu_app_id=str(feishu_bot.get("app_id", "") or ""),
        feishu_app_secret=str(feishu_bot.get("app_secret", "") or ""),
        feishu_default_project=str(feishu_bot.get("default_project", "") or ""),
        feishu_command_prefix=str(feishu_bot.get("command_prefix", "") or ""),
        config_file_path=config_file_path,
    )
