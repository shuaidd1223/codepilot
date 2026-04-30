"""Task-template schema and guide rendering for external AI planners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codepilot.ai_support.agent_commands import _cmd, normalize_command_name
from codepilot.core.task_template import required_task_template_headings

TASK_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "task-template.md"


def _task_template_markdown() -> str:
    """Load the canonical task-template.md content."""
    return TASK_TEMPLATE_PATH.read_text(encoding="utf-8", errors="replace")


def _task_template_example_content() -> str:
    """Render an import-ready example from the canonical task template."""

    replacements = {
        "title": "Builder 子进程日志实时推送",
        "agent": "dual",
        "priority": "P0",
        "depends_on": "T3",
        "risk_level": "中",
        "scope_budget": "最多 3 个文件、约 180 行",
        "owner": "未指派",
        "goal": "让 Builder 子进程 stdout/stderr 能实时转发到 progress_bus，并在 Web UI 任务详情中持续显示最新日志。",
        "builder_responsibilities": (
            "- 捕获 Builder 子进程 stdout/stderr 增量输出\n"
            "- 将日志增量发布为 progress_bus 事件\n"
            "- 在 Web UI 任务详情实时追加日志"
        ),
        "not_in_scope": (
            "- 不重构执行器调度流程\n"
            "- 不修改任务数据库 schema"
        ),
        "forbidden": (
            "- 不提交密钥、令牌或本机路径配置\n"
            "- 不吞掉子进程退出码或失败异常"
        ),
        "files": (
            "- `codepilot/commands/run_live_runner.py`\n"
            "- `codepilot/core/progress_bus.py`\n"
            "- `codepilot/web/components/TaskDetail.js`"
        ),
        "evidence": "现有任务详情依赖 progress_bus 事件刷新，Builder 子进程输出需要通过同一通道进入 Web UI。",
        "notes": (
            "- 实时日志转发要避免阻塞子进程退出\n"
            "- 失败路径仍需保留原始退出码和错误信息"
        ),
        "criteria": (
            "- [ ] Builder 运行时 stdout/stderr 能在任务详情中持续追加显示\n"
            "- [ ] 子进程失败时仍保留原始退出码和错误信息"
        ),
        "ac_matrix": (
            "| AC | Command | Expected | Evidence |\n"
            "| :--- | :--- | :--- | :--- |\n"
            "| AC1 | `pytest tests/test_run_orchestrator.py -q` | 通过并覆盖实时日志事件 | pytest 输出 |\n"
            "| AC2 | `pytest tests/test_webui_api.py -q` | 通过并覆盖任务详情刷新 | pytest 输出 |"
        ),
        "reviewer_responsibilities": (
            "- 检查日志转发不会阻塞子进程退出\n"
            "- 检查失败路径仍能展示错误并保留退出码"
        ),
    }
    content = _task_template_markdown()
    for name, value in replacements.items():
        content = content.replace("{" + name + "}", value)
    return content


def task_template_schema(*, command_name: str = "codepilot") -> dict[str, Any]:
    """Return a machine-readable description of the task template.

    Intended for external AIs that plan tasks themselves and submit finished
    content to CodePilot via `add -f tasks.json`.
    """
    command = normalize_command_name(command_name)
    placeholders = [
        {"name": "title", "required": True, "type": "string",
         "description": "任务标题。动宾结构，能准确表达本任务交付物，不要用『优化』『完善』等泛词。",
         "example": "Builder 子进程 stdout 实时 tail 并通过 progress_bus 转推给 CLI 与 Web"},
        {"name": "agent", "required": False, "type": "string", "default": "dual",
         "description": "执行该任务的智能体身份：builder / reviewer / dual。dual 表示 builder 与 reviewer 闭环。",
         "example": "dual"},
        {"name": "priority", "required": False, "type": "string", "default": "P2",
         "description": "优先级：P0 / P1 / P2 / P3。",
         "example": "P1"},
        {"name": "depends_on", "required": False, "type": "string", "default": "无",
         "description": "依赖的上游任务，如 T1, T3；没有则填『无』。",
         "example": "T2"},
        {"name": "risk_level", "required": False, "type": "string", "default": "待评估",
         "description": "风险等级：低 / 中 / 高 / 待评估。",
         "example": "中"},
        {"name": "scope_budget", "required": False, "type": "string", "default": "未设定",
         "description": "改动预算，例如『最多 3 个文件、200 行』。",
         "example": "最多 2 个文件、约 120 行"},
        {"name": "owner", "required": False, "type": "string", "default": "未指派",
         "description": "负责人；未指派留『未指派』。"},
        {"name": "goal", "required": True, "type": "string",
         "description": "任务目标。一句或两句话阐明『要把什么变成什么』。",
         "example": "让每次 LLM 调用都能把 token 与 elapsed 以 heartbeat 事件推送给 progress_bus。"},
        {"name": "builder_responsibilities", "required": True, "type": "markdown_list",
         "description": "In Scope：builder 必须完成的事项，列表形式，每条动宾结构。"},
        {"name": "not_in_scope", "required": True, "type": "markdown_list",
         "description": "Out of Scope：本任务不允许触碰的范围，防止 builder 越界。"},
        {"name": "forbidden", "required": True, "type": "markdown_list",
         "description": "硬边界：绝对不能做的事（例如不得新增迁移、不得修改公共接口）。"},
        {"name": "files", "required": False, "type": "markdown_list",
         "description": "Files In Scope：允许修改的文件或目录列表。"},
        {"name": "evidence", "required": True, "type": "string",
         "description": "Planning Evidence：规划依据（引用 recon 结果、项目上下文、已有任务）。留空会被质量门标记为纯脑补任务。",
         "example": "recon 显示 ai_gateway_api.py:96 使用阻塞调用；progress_bus.py 已支持 emit(extra=...)；任务池无重叠条目。"},
        {"name": "notes", "required": False, "type": "string",
         "description": "Risks & Notes：执行中需要注意的风险和前置条件。"},
        {"name": "criteria", "required": True, "type": "markdown_list",
         "description": "Acceptance Criteria：可验收的结果列表，每条都要能被客观验证。"},
        {"name": "ac_matrix", "required": True, "type": "markdown_table",
         "description": "Verification Matrix：每条 AC 对应的验证命令、期望结果、证据位置表格。执行阶段填充。"},
        {"name": "reviewer_responsibilities", "required": True, "type": "markdown_list",
         "description": "Reviewer Checkpoints：审查员必须核对的事项列表。"},
    ]
    batch_fields = [
        {"name": "title", "required": True, "type": "string", "description": "任务标题，作为唯一必填字段。"},
        {"name": "content", "required": True, "type": "string",
         "aliases": ["body", "description"],
         "description": "完整的任务正文 markdown。若提供则跳过 AI 生成；建议按 task-template.md 渲染后填入。JSON 批量导入时强烈建议直接提供该字段。"},
        {"name": "agent", "required": False, "type": "string",
         "description": "覆盖命令行 -a；可用 dual / claude / codex / openai-gpt4o 等。"},
        {"name": "priority", "required": False, "type": "string",
         "description": "P0 / P1 / P2 / P3，默认 P2。"},
        {"name": "depends", "required": False, "type": "array|string|int",
         "aliases": ["depends_on", "dependsOn"],
         "description": "依赖的 task id 列表，可传数组、逗号分隔字符串或单个整数。"},
    ]
    batch_example_content = _task_template_example_content()
    return {
        "template_path": str(TASK_TEMPLATE_PATH),
        "template_markdown": _task_template_markdown(),
        "language": {
            "scaffolding": "English",
            "placeholders": "Chinese",
            "note": "模板骨架保持英文，所有占位符（标题、目标、验收标准、备注、职责等）必须用中文，与 task_breakdown.md 的语言规则一致。",
        },
        "placeholders": placeholders,
        "validation": {
            "content_required_for_batch": True,
            "required_headings": required_task_template_headings(),
            "placeholder_tokens": ["{" + item["name"] + "}" for item in placeholders],
            "priority_values": ["P0", "P1", "P2", "P3"],
            "batch_required_fields": ["title", "content"],
            "notes": [
                "批量导入的 content 必须至少包含所有必需章节标题。",
                "如果正文里仍保留 `{goal}`、`{criteria}` 这类模板占位符，说明模板还没填完，应该先修正再导入。",
            ],
        },
        "batch_import": {
            "command": _cmd(command, "add -p <项目名> -f <tasks.json|tasks.md|tasks.txt>"),
            "description": "支持三种格式：JSON 数组（每条带 content）、Markdown 多任务串联、纯文本（每行一个标题，逐条 AI 生成）。所有路径都会做 task-template 合规校验，没有占位通道。",
            "fields": batch_fields,
            "example": [
                {
                    "title": "Builder 子进程日志实时推送",
                    "priority": "P0",
                    "agent": "dual",
                    "content": batch_example_content,
                    "depends": [3],
                }
            ],
            "notes": [
                "JSON 批量：每条必须自带模板合规 content（按 ai template --format json 给出的 schema），缺章节直接拒绝。",
                "Markdown 批量：每个任务都是完整 task-template；多个任务之间用 `---` 串联，且分隔线后紧跟下一个一级标题。",
                "纯文本批量：每行一个标题，CodePilot 逐行调用 --agent 指定的模型生成 content；生成失败或缺章节同样会拒绝整批，绝不静默写入空任务。",
                "content 中的语言应为中文；模板骨架（章节名）保持英文。",
                "人工不应直接调 add；要批量管理任务请走 `python -m codepilot \"需求文本\"` 由规划器拆分。",
            ],
        },
        "filling_rules": [
            "所有占位符内容必须用中文书写，仅模板骨架保留英文。",
            "goal / criteria / builder_responsibilities 必须具体可验证；避免『尽量』『如果可能』等模糊措辞。",
            "evidence 留空会被质量门标记为 fabricated planning；即便是从外部 AI 规划，也要填入你获取到的上下文依据。",
            "risk_level 为『高』时，Reviewer Checkpoints 必须额外列出回滚验证步骤。",
            "Forbidden / Out of Scope 要明确写出，Reviewer 会据此判定 builder 是否越界。",
        ],
        "see_also": [
            _cmd(command, "ai manifest"),
            _cmd(command, "ai guide"),
            _cmd(command, "add -p <项目名> -f <tasks.json>"),
            _cmd(command, "add -p <项目名> -f <tasks.md>"),
        ],
    }


def task_template_schema_json(
    *,
    indent: int = 2,
    command_name: str = "codepilot",
) -> str:
    """Serialize task_template_schema() as JSON."""
    return json.dumps(
        task_template_schema(command_name=command_name),
        ensure_ascii=False,
        indent=indent,
    )


def task_template_guide_markdown(*, command_name: str = "codepilot") -> str:
    """Return a Chinese-language filling guide aimed at external AI planners."""
    command = normalize_command_name(command_name)
    schema = task_template_schema(command_name=command)
    placeholder_rows = "\n".join(
        f"| `{{{item['name']}}}` | {'是' if item.get('required') else '否'} | "
        f"{item.get('type', '-')} | {item['description']}"
        + (f"<br/>*示例：{item['example']}*" if item.get('example') else "")
        + " |"
        for item in schema["placeholders"]
    )
    batch_rows = "\n".join(
        f"| `{item['name']}` | {'是' if item.get('required') else '否'} | "
        f"{item.get('type', '-')} | {item['description']}"
        + (f"<br/>*别名：{', '.join(item['aliases'])}*" if item.get('aliases') else "")
        + " |"
        for item in schema["batch_import"]["fields"]
    )
    rules = "\n".join(f"- {rule}" for rule in schema["filling_rules"])
    example_json = json.dumps(
        schema["batch_import"]["example"], ensure_ascii=False, indent=2
    )
    return f"""# CodePilot 任务模板填充指南

这份指南写给**外部 AI 规划器**：你在自己的流程里拆出任务后，按本指南把结果渲染成 CodePilot 可接收的格式，然后用 `{_cmd(command, 'add -p <项目名> -f <tasks.json>')}` 批量投递。若你已经把每条任务渲染成完整 task-template markdown，也可以改用 `{_cmd(command, 'add -p <项目名> -f <tasks.md>')}`，多个任务之间用 `---` 分隔。

## 语言规则

- 模板骨架（章节名、标签）保持英文；
- 所有占位符内容（标题、目标、验收标准、职责、备注等）使用**中文**。

## 模板占位符

完整模板可用 `{_cmd(command, 'ai template')}` 直接读取。下面是每个占位符的填充规则：

| 占位符 | 必填 | 类型 | 说明 |
| :--- | :--- | :--- | :--- |
{placeholder_rows}

## 批量导入 JSON 格式

用命令 `{_cmd(command, 'add -p <项目名> -f <tasks.json>')}`。文件内容是一个对象数组，每项字段：

| 字段 | 必填 | 类型 | 说明 |
| :--- | :--- | :--- | :--- |
{batch_rows}

### 最小示例

```json
{example_json}
```

### 注意

{chr(10).join(f"- {note}" for note in schema["batch_import"]["notes"])}

## 填充硬规则

{rules}

## 相关命令

- `{_cmd(command, 'ai template')}` — 输出原始 task-template.md
- `{_cmd(command, 'ai template --format json')}` — 输出本 schema 的机器可读版本
- `{_cmd(command, 'ai manifest')}` — 完整命令清单
"""
