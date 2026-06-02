"""Friendly error messages and actionable suggestions for CodePilot."""

from __future__ import annotations

import sys

from rich.console import Console

from codepilot.core.config import ConfigError as RuntimeConfigError
from codepilot.errors import CodePilotError, ProviderError
from codepilot.errors import ConfigError as LegacyConfigError

_stderr_console = Console(file=sys.stderr, stderr=True, highlight=False)


CONFIG_ERROR_TYPES = (RuntimeConfigError, LegacyConfigError)


def format_error(exc: Exception, context: str = "") -> str:
    """Format an exception into a user-friendly error message with actionable suggestions.

    Args:
        exc: The exception to format.
        context: Optional context information about where the error occurred.

    Returns:
        A formatted error message string with suggestions.
    """
    if isinstance(exc, CONFIG_ERROR_TYPES):
        return _format_config_error(exc, context)
    if isinstance(exc, ProviderError):
        return _format_provider_error(exc, context)
    if isinstance(exc, CodePilotError):
        return _format_codepilot_error(exc, context)
    return _format_unknown_error(exc, context)


def _format_config_error(exc: Exception, context: str) -> str:
    """Format ConfigError with actionable suggestions."""
    suggestions = [
        "运行 `codepilot doctor` 检查配置",
        "运行 `codepilot config init` 重新初始化配置",
        "手动编辑 `.codepilot/AGENTS.toml`",
    ]
    context_str = f" [{context}]" if context else ""
    return f"[red]配置错误[/red]{context_str}: {exc}\n" + "\n".join(f"  • {s}" for s in suggestions)


def _format_provider_error(exc: ProviderError, context: str) -> str:
    """Format ProviderError with actionable suggestions."""
    suggestions = [
        "检查 API Key 配置",
        "运行 `codepilot exec --dry-run` 测试连接",
        "确认 base_url 配置正确",
    ]
    context_str = f" [{context}]" if context else ""
    return f"[yellow]Provider 错误[/yellow]{context_str}: {exc}\n" + "\n".join(f"  • {s}" for s in suggestions)


def _format_codepilot_error(exc: CodePilotError, context: str) -> str:
    """Format generic CodePilotError."""
    context_str = f" [{context}]" if context else ""
    return f"[red]CodePilot 错误[/red]{context_str}: {exc}"


def _format_unknown_error(exc: Exception, context: str) -> str:
    """Format unknown exception."""
    context_str = f" [{context}]" if context else ""
    return f"[red]未知错误[/red]{context_str}: {exc}"


def suggest_fix(exc: Exception) -> list[str]:
    """Return a list of actionable suggestions for the given exception.

    Args:
        exc: The exception to analyze.

    Returns:
        A list of suggestion strings.
    """
    if isinstance(exc, CONFIG_ERROR_TYPES):
        return [
            "运行 `codepilot doctor` 检查配置",
            "运行 `codepilot config init` 重新初始化配置",
            "手动编辑 `.codepilot/AGENTS.toml`",
        ]
    if isinstance(exc, ProviderError):
        return [
            "检查 API Key 配置",
            "运行 `codepilot exec --dry-run` 测试连接",
            "确认 base_url 配置正确",
        ]
    if isinstance(exc, FileNotFoundError):
        return [
            f"检查文件路径是否存在：{exc}",
            "确认文件权限正确",
        ]
    return [
        "查看日志获取详细信息",
        "运行 `codepilot doctor` 检查环境",
    ]


def error_handler(func):
    """Decorator that catches exceptions and formats them with friendly messages.

    Usage::

        @error_handler
        def my_function():
            # ... may raise exceptions ...
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except CodePilotError as exc:
            _stderr_console.print(format_error(exc, context=func.__name__))
            return None
    return wrapper
