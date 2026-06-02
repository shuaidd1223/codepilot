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
    """不支持的 Agent 类型（由 ``mcp.launchers`` 导入使用）。"""


class PromptNotFoundError(CodePilotError, LookupError):
    """找不到请求的 Prompt 模板（由 ``prompts`` 导入使用）。"""


# ── 存储错误 ───────────────────────────────────────────────────────────

class StorageError(CodePilotError):
    """数据库/文件存储错误。"""


# ── 构建/发布错误 ──────────────────────────────────────────────────────

class BuildError(CodePilotError):
    """二进制打包或安装错误。"""


class BuildFixError(CodePilotError, RuntimeError):
    """build-fix 闭环失败（由 ``commands.build_fix`` 导入使用）。"""


# ── 外部集成错误 ───────────────────────────────────────────────────────

class VendorFetcherError(CodePilotError, RuntimeError):
    """Vendor 二进制下载/校验错误（由 ``binary_support.vendor_fetcher`` 导入使用）。"""


class UnsupportedPlatformError(VendorFetcherError):
    """不支持的平台/架构。"""


class ChecksumMismatchError(VendorFetcherError):
    """下载文件校验和不匹配。"""


class HookError(CodePilotError):
    """Webhook / 事件插件执行错误（由 ``core.hook_registry`` / ``core.event_plugins`` 导入使用）。"""


# ── CLI 执行错误 ───────────────────────────────────────────────────────

class ExecError(CodePilotError, ValueError):
    """CLI 命令执行失败（由 ``commands.exec_cmd`` 导入使用）。"""


class SelfUpdateError(CodePilotError, ValueError):
    """self-update 流程错误（由 ``commands.self_update`` 导入使用）。"""


class SetupError(CodePilotError, ValueError):
    """项目初始化配置错误（由 ``commands.setup`` 导入使用）。"""


class WikiError(CodePilotError, ValueError):
    """Wiki 操作错误（由 ``commands.wiki`` 导入使用）。"""


class NoteError(CodePilotError, ValueError):
    """记事本操作错误（由 ``commands.note`` 导入使用）。"""


class SkillCatalogError(CodePilotError, ValueError):
    """Skill 目录加载错误（由 ``commands.skill_catalog`` 导入使用）。"""


class CronExpressionError(CodePilotError, ValueError):
    """Cron 表达式解析错误（由 ``scheduled.triggers`` 导入使用）。"""


class PreflightSkipError(CodePilotError, RuntimeError):
    """执行前检查跳过（由 ``commands.run_shell`` 导入使用）。"""


# ── MCP 错误 ───────────────────────────────────────────────────────────

class MCPError(CodePilotError):
    """MCP 协议/工具调用错误。"""


class CodePilotToolError(MCPError, Exception):
    """MCP 工具调用失败（由 ``mcp.protocol`` 导入使用）。"""


# ── Web UI 错误 ────────────────────────────────────────────────────────

class WebUIError(CodePilotError):
    """Web UI 操作错误。"""
