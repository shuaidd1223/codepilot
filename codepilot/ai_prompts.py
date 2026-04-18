"""Prompt templates and JSON schemas used by planner / classifier."""

from __future__ import annotations

from dataclasses import dataclass, field


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




TASK_PROMPT_TEMPLATE = """\
你是一个智能体任务规划助手。你的职责是根据用户提供的任务标题，生成一份结构化的任务描述文档。

**输出要求**：直接输出 Markdown 内容，不需要任何开场白、解释、或"以下是..."之类的废话。

**必须包含以下 6 个 section**（每个都要有实质内容）：

### 任务目标
清晰描述这个任务要做什么：输入、过程、输出各是什么。

### 验收标准
至少 3 条具体可验证的标准。能用命令验证的写命令，能直接看文件判断的写文件路径。

### Builder 职责（针对代码生成）
具体列出：需要读哪些文件、创建/修改哪些文件、怎样实现。

### Reviewer 职责（针对代码审查）
具体列出：需要 review 哪些内容、验证哪些方面、用什么标准判断通过。

### 涉及文件
列出任务预计修改的文件路径（相对于项目根目录）。只列真实可能的路径。

### 备注
前置条件、风险、注意事项（1-3 条即可）。

---
任务标题: {title}
{project_context}
"""


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


TASK_BREAKDOWN_PROMPT_TEMPLATE = (
    "你是一个任务拆解器. 用户给了你一个明确的需求, 你必须把它拆成可执行的工程子任务.\n"
    "\n"
    "核心原则:\n"
    "- 你的所有任务必须 100% 围绕用户的需求, 不能偏离.\n"
    "- 禁止自作主张去修 bug、优化代码、重构架构, 除非用户明确要求.\n"
    "- 禁止分析项目现有问题然后去修, 那不是你的工作.\n"
    "- 如果用户说 '重构 WebUI 为 Vue', 你的所有任务都必须是关于 Vue 重构的.\n"
    "\n"
    "规则:\n"
    "1. 判断 simple 或 complex. simple=1个任务, complex=2到{max_tasks}个.\n"
    "2. 每个任务标题必须直接体现用户需求中的关键动作.\n"
    "3. 每个任务要具体到: 改哪些文件, 怎么改, 验收标准是什么.\n"
    "4. files 只写与用户需求直接相关的文件路径.\n"
    "5. 只输出 JSON, 不要输出 Markdown 或解释.\n"
    "6. summary 必须是对用户需求的一句话概括.\n"
    "7. 禁止出现 '等待输入' '请提供' 'awaiting' 'placeholder' 等无意义标题.\n"
    "\n"
    "=== 用户需求 ===\n"
    "{title}\n"
    "\n"
    "{project_context}\n"
)



