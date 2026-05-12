"""CodePilot 统一错误层次结构。

所有项目内部异常都继承自 ``CodePilotError``，避免调用方需要
同时捕获 ``RuntimeError``、``ValueError``、``LookupError`` 等
不同的内置异常类型。
"""

from __future__ import annotations


class CodePilotError(Exception):
    """所有 CodePilot 异常的基类。"""


# ── 配置错误 ───────────────────────────────────────────────────────────

class ConfigError(CodePilotError, ValueError):
    """AGENTS.toml / 项目配置解析错误。已被 ``core.config.ConfigError`` 兼容。"""


# ── Provider / AI 错误 ─────────────────────────────────────────────────

class ProviderError(CodePilotError):
    """AI Provider 连接或密钥配置错误。"""


class UnsupportedAgentError(CodePilotError, ValueError):
    """不支持的 Agent 类型。已在 ``codepilot/__init__.py`` 中定义。"""


class PromptNotFoundError(CodePilotError, LookupError):
    """找不到请求的 Prompt 模板。已在 ``codepilot/__init__.py`` 中定义。"""


# ── 存储错误 ───────────────────────────────────────────────────────────

class StorageError(CodePilotError):
    """数据库/文件存储错误。"""


# ── 构建/发布错误 ──────────────────────────────────────────────────────

class BuildError(CodePilotError):
    """二进制打包或安装错误。"""


class BuildFixError(CodePilotError, RuntimeError):
    """build-fix 闭环失败。已在 ``commands/build_fix.py`` 中定义。"""


# ── 外部集成错误 ───────────────────────────────────────────────────────

class VendorFetcherError(CodePilotError, RuntimeError):
    """Vendor 二进制下载/校验错误。已在 ``binary_support/vendor_fetcher.py`` 中定义。"""


class UnsupportedPlatformError(VendorFetcherError):
    """不支持的平台/架构。"""


class ChecksumMismatchError(VendorFetcherError):
    """下载文件校验和不匹配。"""


class HookError(CodePilotError):
    """Webhook / 事件插件执行错误。已在 ``core/hook_registry.py`` / ``core/event_plugins.py`` 中定义。"""


# ── CLI 执行错误 ───────────────────────────────────────────────────────

class ExecError(CodePilotError, ValueError):
    """CLI 命令执行失败。已在 ``commands/exec_cmd.py`` 中定义。"""


class SelfUpdateError(CodePilotError, ValueError):
    """self-update 流程错误。已在 ``commands/self_update.py`` 中定义。"""


class SetupError(CodePilotError, ValueError):
    """项目初始化配置错误。已在 ``commands/setup.py`` 中定义。"""


class WikiError(CodePilotError, ValueError):
    """Wiki 操作错误。已在 ``commands/wiki.py`` 中定义。"""


class NoteError(CodePilotError, ValueError):
    """记事本操作错误。已在 ``commands/note.py`` 中定义。"""


class SkillCatalogError(CodePilotError, ValueError):
    """Skill 目录加载错误。已在 ``commands/skill_catalog.py`` 中定义。"""


class CronExpressionError(CodePilotError, ValueError):
    """Cron 表达式解析错误。已在 ``scheduled/triggers.py`` 中定义。"""


class PreflightSkipError(CodePilotError, RuntimeError):
    """执行前检查跳过。已在 ``commands/run_shell.py`` 中定义。"""


# ── MCP 错误 ───────────────────────────────────────────────────────────

class MCPError(CodePilotError):
    """MCP 协议/工具调用错误。"""


class CodePilotToolError(MCPError, Exception):
    """MCP 工具调用失败。已在 ``mcp/protocol.py`` 中定义。"""


# ── Web UI 错误 ────────────────────────────────────────────────────────

class WebUIError(CodePilotError):
    """Web UI 操作错误。"""
