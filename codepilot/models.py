"""Pydantic 数据模型：Task / Project / TaskLog."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class AgentMode(str, Enum):
    # CLI Agents
    CODEX = "codex"
    CLAUDE = "claude"
    CLAUDE_NODE = "claude-node"
    GEMINI = "gemini"
    CLOUD = "cloud"
    # API Agents
    OPENAI_GPT4 = "openai-gpt4"
    OPENAI_GPT4O = "openai-gpt4o"
    OPENAI_GPT35 = "openai-gpt35"
    CLAUDE_OPUS = "claude-opus"
    CLAUDE_SONNET = "claude-sonnet"
    CLAUDE_HAIKU = "claude-haiku"
    HUNYUAN = "hunyuan"
    ZHIPU_GLM4 = "zhipu-glm4"
    WENXIN = "wenxin"
    QWEN = "qwen"
    DEEPSEEK = "deepseek"
    OLLAMA = "ollama"
    GROQ = "groq"
    # Special modes
    DUAL = "dual"  # Builder + Reviewer 双代理模式
    # Aliases for backward compatibility
    GPT4 = "openai-gpt4"
    GPT4O = "openai-gpt4o"
    GPT35 = "openai-gpt35"
    SONNET = "claude-sonnet"
    OPUS = "claude-opus"
    HAIKU = "claude-haiku"

    @classmethod
    def get_alias(cls, value: str) -> "AgentMode":
        """解析别名，返回标准枚举值."""
        aliases = {
            "gpt4": cls.OPENAI_GPT4,
            "gpt-4": cls.OPENAI_GPT4,
            "gpt4o": cls.OPENAI_GPT4O,
            "gpt-4o": cls.OPENAI_GPT4O,
            "gpt35": cls.OPENAI_GPT35,
            "gpt-3.5": cls.OPENAI_GPT35,
            "sonnet": cls.CLAUDE_SONNET,
            "opus": cls.CLAUDE_OPUS,
            "haiku": cls.CLAUDE_HAIKU,
            "混元": cls.HUNYUAN,
            "glm": cls.ZHIPU_GLM4,
            "glm4": cls.ZHIPU_GLM4,
            "文心": cls.WENXIN,
            "ernie": cls.WENXIN,
            "通义": cls.QWEN,
            "deepseek": cls.DEEPSEEK,
            "ollama": cls.OLLAMA,
            "groq": cls.GROQ,
        }
        return aliases.get(value.lower(), cls(value))


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Project(BaseModel):
    """项目模型."""
    name: str
    path: str
    base_branch: str = "dev"
    default_mode: AgentMode = AgentMode.DUAL
    worktree_base: Optional[str] = None
    config_file: Optional[str] = None
    created_at: Optional[str] = None


class Task(BaseModel):
    """任务模型."""
    id: Optional[int] = None
    project: str
    title: str
    content: str = ""
    agent: AgentMode = AgentMode.DUAL
    builder: Optional[str] = None
    reviewer: Optional[str] = None
    priority: Priority = Priority.P2
    depends_on: Optional[list[int]] = None  # JSON 存储
    status: TaskStatus = TaskStatus.BACKLOG
    project_path: str
    branch_name: Optional[str] = None
    worktree_path: Optional[str] = None
    error_message: Optional[str] = None
    delivery_record: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: dict) -> "Task":
        """从数据库行创建 Task 实例，自动解析 JSON 字段."""
        if row.get("depends_on"):
            try:
                row["depends_on"] = json.loads(row["depends_on"])
            except (json.JSONDecodeError, TypeError):
                row["depends_on"] = None
        # 解析枚举
        row["status"] = TaskStatus(row.get("status", "backlog"))
        row["agent"] = AgentMode(row.get("agent", "dual"))
        row["priority"] = Priority(row.get("priority", "P2"))
        return cls(**row)


class TaskLog(BaseModel):
    """任务执行日志模型."""
    id: Optional[int] = None
    task_id: int
    agent: Optional[str] = None
    phase: str  # builder / reviewer
    output: str = ""
    exit_code: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration: Optional[int] = None  # 秒


class TaskStats(BaseModel):
    """任务统计."""
    backlog: int = 0
    in_progress: int = 0
    done: int = 0
    failed: int = 0
    total: int = 0
