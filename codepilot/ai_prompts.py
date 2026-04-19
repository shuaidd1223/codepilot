"""Prompt templates and JSON schemas used by planner / classifier.

The prose bodies of these templates now live in :mod:`codepilot.prompts`
(one ``.md`` file per template) so they can be tweaked without touching
Python. This module still owns the JSON schemas (which are data contracts
consumed both by the CLI ``--json-schema`` flag and by tests) and the
``AgentConfig`` dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codepilot.prompts import load_prompt as _load_prompt


class AgentConfig:
    """Agent 运行配置."""
    builder: str = "codex"  # CLI name 或 API provider key
    reviewer: str = "codex"
    mode: str = "dual"  # "cli" | "api" | "dual"
    # API 密钥（可以从环境变量覆盖）
    api_keys: dict[str, str] = field(default_factory=dict)

    def resolve_builder(self) -> tuple[str, str]:
        """解析 builder，返回 (type, name)."""
        if self.builder in API_PROVIDERS:
            return "api", self.builder
        return "cli", self.builder

    def resolve_reviewer(self) -> tuple[str, str]:
        """解析 reviewer，返回 (type, name)."""
        if self.reviewer in API_PROVIDERS:
            return "api", self.reviewer
        return "cli", self.reviewer




# Kept as module-level strings for backward compatibility with callers that
# imported them directly; callers that build their final prompt with
# `.format()` continue to work unchanged.
TASK_PROMPT_TEMPLATE = _load_prompt("task_single")


TASK_BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "complexity": {"type": "string", "enum": ["simple", "complex"]},
        "should_split": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "goal": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                    },
                    "builder_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reviewer_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "depends_on_indices": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "description": "依赖哪些子任务，按 tasks 数组下标；留空表示可以和其它无依赖任务并行",
                    },
                },
                "required": [
                    "title",
                    "priority",
                    "goal",
                    "acceptance_criteria",
                    "builder_notes",
                    "reviewer_notes",
                    "files",
                    "notes",
                    "depends_on_indices",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "complexity", "should_split", "tasks"],
    "additionalProperties": False,
}


RECON_SCHEMA = {
    "type": "object",
    "properties": {
        "current_state": {
            "type": "string",
            "description": "一句话描述当前项目与这个需求相关的现状。",
        },
        "relevant_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "已经读过或判断需要改的文件路径，相对项目根目录。不要凭空造文件。",
        },
        "key_findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "读代码之后的关键发现：现有实现、相关模块、可能的阻塞点。",
        },
        "risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "风险点：可能打破的地方、测试薄弱区、向后兼容问题。",
        },
        "suggested_approach": {
            "type": "string",
            "description": "高层实现路径：一段话讲清楚打算怎么做。",
        },
    },
    "required": ["current_state", "relevant_files", "key_findings", "suggested_approach"],
    "additionalProperties": False,
}


RECON_PROMPT_TEMPLATE = _load_prompt("task_recon")


TASK_BREAKDOWN_PROMPT_TEMPLATE = _load_prompt("task_breakdown")



