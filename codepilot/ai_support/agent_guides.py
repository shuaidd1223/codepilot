"""Markdown and prompt text for AI-facing CodePilot usage."""

from __future__ import annotations

from codepilot.ai_support.agent_commands import _cmd, normalize_command_name
from codepilot.ai_support.project_metadata import project_metadata_markdown
from codepilot.core.config import normalize_agent_language

def ai_guide_markdown(*, command_name: str = "codepilot", language: str = "en") -> str:
    """Return an AI-oriented Markdown guide."""
    command = normalize_command_name(command_name)
    if normalize_agent_language(language) == "en":
        return f"""# CodePilot AI Usage Guide

Language: [简体中文](AI_USAGE.zh-CN.md) | English

{project_metadata_markdown(language="en")}

This guide is for other AI agents. For the latest machine-readable command list, use `{_cmd(command, "ai manifest")}`. For this Markdown guide, use `{_cmd(command, "ai guide")}`.

## Core Calling Rules

1. Prefer non-interactive commands.
2. Prefer `--json` or `{_cmd(command, "ai manifest")}` when structured output is needed.
3. Submit high-level requirements with `{command} "requirement text"` or `{_cmd(command, 'go "requirement text"')}`.
4. Treat questions about project status, task counts, completion, failed tasks, running tasks, or service status as Q&A.
5. `chat`, Web UI sessions, and Feishu free text enter OpenCode + CodePilot MCP and can receive questions, requirements, or operation intent directly.
6. Use `{_cmd(command, "task ...")}` for task operations.
7. Use `{_cmd(command, "binary ...")}` for releases.
8. External AI systems must read `{_cmd(command, "ai template --format json")}` before submitting tasks directly.
9. Use `clarify` / `plan` explicitly when a deterministic spec or plan artifact is needed.

## Recommended Commands

### Project Setup

```bash
{_cmd(command, "setup . --dry-run --json")}
{_cmd(command, "setup .")}
{_cmd(command, "doctor --project <project-name> --services --json")}
```

### Submit Requirements

```bash
{command} "fix task retry logic and add tests"
{_cmd(command, 'go "fix task retry logic and add tests" -p <project-name>')}
```

### Clarify, Plan, and Safe Next Actions

```bash
{_cmd(command, 'clarify -p <project-name> "vague requirement" --json')}
{_cmd(command, 'plan -p <project-name> "clear requirement" --json')}
{_cmd(command, "inspect -p <project-name> --once --dry-run --write-workflow --json")}
{_cmd(command, "workflow status -p <project-name> --json")}
{_cmd(command, "workflow next -p <project-name> --list --json")}
{_cmd(command, "workflow next -p <project-name> --action <id> --json")}
{_cmd(command, "workflow next -p <project-name> --auto --json")}
```

`clarify`, `plan`, and `inspect --write-workflow` create reviewable artifacts and record `next_actions`, but they do not create backlog tasks or start execution by themselves. Use `workflow next --list` to inspect available actions, `workflow next --action <id>` to execute an allowlisted action, or `workflow next --auto` to let CodePilot choose low-risk policy actions. `suggested_command` is only display/review metadata; do not compose or execute it automatically. By default `--auto` does not create inspect tasks or import plan tasks; projects can opt in with `[automation] workflow_auto_create_inspect_tasks`, `workflow_auto_import_plan_tasks`, `workflow_auto_max_steps`, and `workflow_auto_failure_threshold`. High-risk actions still require explicit confirmation and must pass the workflow next allowlist.

### Status and Evidence

```bash
{_cmd(command, "status -p <project-name> --json")}
{_cmd(command, "hud -p <project-name> --preset full --json")}
{_cmd(command, 'explore -p <project-name> --prompt "find task template" --json')}
{_cmd(command, "trace -p <project-name> --limit 30 --json")}
{_cmd(command, "memory events -p <project-name> --json")}
```

`memory events` reads automatic factual observations from `.codepilot/memory/events.jsonl`. CodePilot turns them into deduplicated candidates and maintains `.codepilot/memory/autocapture.md`; candidates record `score`, `feedback`, and `seen_count`, with workflow actions and terminal task outcomes automatically adjusting weight. It does not directly write human-maintained long-term wiki/note content.

### Task Template

If an external AI submits tasks via `add -f tasks.json` / `add -f tasks.md`, it must read the template first:

```bash
{_cmd(command, "ai template")}
{_cmd(command, "ai template --format json")}
{_cmd(command, "ai template --format guide")}
```

Required rules:

1. Humans should use `{command} "requirement text"` instead of direct `add`.
2. Each `tasks.json` item must include template-compliant `content`.
3. Each `tasks.md` section must be a complete task template.
4. `add -t "title"` and `tasks.txt` call AI to generate content and validate it.
5. `--no-ai` / `--allow-empty` are removed; empty placeholder tasks are not allowed.
"""
    return f"""# CodePilot AI 调用手册

语言版本：中文 | [English](AI_USAGE.en-US.md)

{project_metadata_markdown(language="zh-CN")}

这份手册是写给其他 AI / Agent 的静态入口。最新机器可读清单以 `{_cmd(command, "ai manifest")}` 为准，最新 Markdown 手册以 `{_cmd(command, "ai guide")}` 为准。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时优先使用 `--json` 或 `{_cmd(command, "ai manifest")}`。
3. 提交高层需求时直接调用 `{command} "需求文本"` 或 `{_cmd(command, 'go "需求文本"')}`。
4. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态这类问题应作为问答处理。
5. `chat`、Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP，可直接输入问题、需求或操作意图。
6. 任务运维统一使用 `{_cmd(command, "task ...")}`。
7. 发布统一使用 `{_cmd(command, "binary ...")}`。
8. 外部 AI 直接投递任务前必须读取 `{_cmd(command, "ai template --format json")}`。
9. 需要规格或计划 artifact 时显式调用 `clarify` / `plan`。

## 推荐命令

### 1. 项目准备

```bash
{_cmd(command, "setup . --dry-run --json")}
{_cmd(command, "setup .")}
{_cmd(command, "doctor --project <项目名> --services --json")}
```

### 2. 提交需求

```bash
{command} "修复任务重试逻辑并补测试"
{_cmd(command, 'go "修复任务重试逻辑并补测试" -p <项目名>')}
```

### 3. 澄清和计划

```bash
{_cmd(command, 'clarify -p <项目名> "模糊需求" --json')}
{_cmd(command, 'plan -p <项目名> "明确需求" --json')}
{_cmd(command, "plan -p <项目名> --from-spec .codepilot/specs/example.md --json")}
{_cmd(command, "inspect -p <项目名> --once --dry-run --write-workflow --json")}
{_cmd(command, "workflow status -p <项目名> --json")}
{_cmd(command, "workflow next -p <项目名> --list --json")}
{_cmd(command, "workflow next -p <项目名> --action <id> --json")}
{_cmd(command, "workflow next -p <项目名> --auto --json")}
```

`clarify`、`plan` 和 `inspect --write-workflow` 不创建 backlog、不启动执行器。

`clarify` 和 `plan` 的 `--json` 输出包含 `next_actions` 字段，列出后续可用操作（生成计划、导入任务、继续澄清、放弃等）。每个 next action 包含 `id`、`label`、`risk` 和 `suggested_command`。外部 AI / Agent 应优先用 `workflow next --list` 查看可用动作，再用 `workflow next --action <id>` 通过固定 allowlist 安全推进；也可以用 `workflow next --auto` 让 CodePilot 自动选择低风险策略动作。`suggested_command` 只用于展示/审查，不作为自动执行源。默认 `--auto` 不创建 inspect 任务、不导入 plan 任务；项目可通过 `[automation] workflow_auto_create_inspect_tasks`、`workflow_auto_import_plan_tasks`、`workflow_auto_max_steps` 和 `workflow_auto_failure_threshold` 放开策略。高风险动作仍需显式确认，且必须在 `workflow next` allowlist 内。

### 4. 状态与证据

```bash
{_cmd(command, "status -p <项目名> --json")}
{_cmd(command, "hud -p <项目名> --preset full --json")}
{_cmd(command, 'explore -p <项目名> --prompt "find task template" --json')}
{_cmd(command, "trace -p <项目名> --limit 30 --json")}
{_cmd(command, 'wiki query -p <项目名> "构建" --json')}
{_cmd(command, "note show -p <项目名> --json")}
{_cmd(command, "memory events -p <项目名> --json")}
```

`explore` 是只读入口，不写文件、不改 Git、不启动服务、不安装依赖、不执行测试。

`memory events` 读取 `.codepilot/memory/events.jsonl` 中的自动观察事实。CodePilot 会自动生成去重候选并维护 `.codepilot/memory/autocapture.md`；候选会记录 `score`、`feedback` 和 `seen_count`，由 workflow action 和任务终态自动升权/降权，但不会直接写人工维护的长期 wiki/note。

### 5. 任务查看与控制

```bash
{_cmd(command, "task show <task_id> --json")}
{_cmd(command, "task logs <task_id> --tail 80")}
{_cmd(command, "task find <关键词> -p <项目名> --json")}
{_cmd(command, "task stop <task_id>")}
{_cmd(command, "task retry <task_id>")}
{_cmd(command, "task resume <task_id>")}
{_cmd(command, "task cancel <task_id>")}
{_cmd(command, "task archive <task_id>")}
{_cmd(command, "task rm <task_id>")}
```

### 6. 执行与修复闭环

```bash
{_cmd(command, "run -p <项目名> --once")}
{_cmd(command, "daemon -p <项目名> --status")}
{_cmd(command, "inspect -p <项目名> --once --json")}
{_cmd(command, "inspect -p <项目名> --once --dry-run --write-workflow --json")}
{_cmd(command, "build-fix -p <项目名> --task-id <task_id> --dry-run")}
{_cmd(command, "build-fix -p <项目名> --task-id <task_id> --json")}
```

普通 `inspect --dry-run` 保持只输出预览；加 `--write-workflow` 才会写 `.codepilot/context/` 和 Agent Session，并通过 `workflow next` 暴露安全动作。

任务失败后，如需完整修复闭环优先用 `build-fix`；只需重新排队时用 `task retry`。

### 7. Web UI、飞书与 Webhook

```bash
{_cmd(command, "ui start")}
{_cmd(command, "ui status")}
{_cmd(command, "ui logs --tail 100")}
{_cmd(command, "feishu start")}
{_cmd(command, "feishu status")}
{_cmd(command, "feishu logs --tail 100")}
{_cmd(command, "webhook --host 127.0.0.1 --port 8765")}
```

飞书自由文本进入当前项目的 OpenCode 会话；没有当前项目时会先返回项目选择卡片。明确命令仍可直接使用：

```text
当前项目状态怎么样
优化飞书任务面板
修复失败任务前先列出候选和风险
tasks failed
retry 123
```

`chat` 启动 CodePilot 管理的 OpenCode TUI。运行时配置、会话数据库和模型选择写入用户级 `~/.codepilot/opencode/<项目标识>/`，与用户自己直接运行的 OpenCode 隔离。

### 8. 事件、Hook、Provider 与 Skill

```bash
{_cmd(command, "event schema --json")}
{_cmd(command, "event list -p <项目名> --json")}
{_cmd(command, "hook validate -p <项目名> --json")}
{_cmd(command, "exec -p <项目名> --provider codex --dry-run --json -- codex --version")}
{_cmd(command, "skill list -p <项目名> --json")}
{_cmd(command, 'skill run ralplan -p <项目名> --provider codex --input "新增 wiki context" --json')}
```

### 9. 发布

```bash
{_cmd(command, "binary prepare --version 0.7.5")}
{_cmd(command, "binary release --build-current")}
{_cmd(command, "binary verify")}
```

## 结构化接口

```bash
{_cmd(command, "ai manifest")}
{_cmd(command, "ai guide")}
{_cmd(command, "ai prompt")}
```

## 任务模板

如果外部 AI 不走 CodePilot 规划器，而是自己规划后通过 `add -f tasks.json` / `add -f tasks.md` 投递，必须先读取模板：

```bash
{_cmd(command, "ai template")}
{_cmd(command, "ai template --format json")}
{_cmd(command, "ai template --format guide")}
```

强制规则：

1. 人工不要直接 `add`，应走 `{command} "需求文本"`。
2. `tasks.json` 每条必须带模板合规 `content`。
3. `tasks.md` 每段必须是完整 task-template。
4. `add -t "标题"` 和 `tasks.txt` 会调用 AI 生成 content 并校验。
5. `--no-ai` / `--allow-empty` 已废弃，没有空 content 占位通道。

## 兼容性

这些旧入口已移除：

- `{_cmd(command, "release ...")}`
- 顶层 `{_cmd(command, "show/logs/stop/retry/find/...")}`
- `{_cmd(command, "webui ...")}`
- `{_cmd(command, "chat --no-ui")}`
- `--no-ai` / `--allow-empty`

统一改用：

- `{_cmd(command, "binary ...")}`
- `{_cmd(command, "task ...")}`
- `{_cmd(command, "ui <start|status|logs|stop|restart>")}`
"""
    return f"""# CodePilot AI 调用手册

这份手册是写给其他 AI/Agent 的，不是写给人类终端用户的。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时，优先使用 JSON 输出命令。
3. 需要提交高层需求时，直接调用自然语言入口，不要先自己拆任务，除非你明确要控制拆分策略。
4. 看到任务处于 `in_progress` 时，先查 `status -v` 和 `task logs`，不要盲目重复触发 `run`。
5. 任务失败后，如需修复闭环优先使用 `{_cmd(command, "build-fix -p <项目名> --task-id <task_id> --json")}`；只需人工重新排队时使用 `{_cmd(command, "task retry <task_id>")}`。
6. 准备发布包时，优先使用 `{_cmd(command, "binary prepare --version <版本号>")}`。
7. `chat`、Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP，可直接输入问题、需求或操作意图。
8. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态这类问题应作为问答处理；CodePilot 会优先读取本地运行数据。

## 推荐命令

### 1. 初始化项目

```bash
{_cmd(command, "setup .")}
```

### 2. 提交一个需求

```bash
{command} "修复任务重试逻辑并补测试"
```

或者显式：

```bash
{command} go "修复任务重试逻辑并补测试"
```

### 3. 查看任务状态

人类可读：

```bash
{_cmd(command, "status -p <项目名> -v")}
```

机器可读：

```bash
{_cmd(command, "status -p <项目名> --json")}
```

### 3.1 查看轻量 HUD

```bash
{_cmd(command, "hud -p <项目名> --preset full")}
{_cmd(command, "hud -p <项目名> --preset full --json")}
```

`hud` 适合快速判断当前工作台是否繁忙：它汇总项目队列、运行中任务、最近活动和后台服务状态；需要实时观察时使用 `hud --watch`。

### 3.2 只读探索项目证据

```bash
{_cmd(command, 'explore --prompt "find task template" --use-wiki --json')}
```

`explore` 只读取项目文件、Git、wiki、任务日志摘要和 inspect 信号。涉及修改、安装、启动服务或执行测试的问题应改走普通 workflow。

### 3.3 项目本地 wiki

```bash
{_cmd(command, 'wiki add -p <项目名> --title "构建命令" --body "pytest tests"')}
{_cmd(command, 'wiki query -p <项目名> "构建" --json')}
{_cmd(command, 'wiki update -p <项目名> --slug build --body "pytest -q" --json')}
{_cmd(command, "wiki refresh -p <项目名> --json")}
{_cmd(command, "wiki ingest --from trace -p <项目名> --json")}
{_cmd(command, "wiki ingest --from plan -p <项目名> --json")}
{_cmd(command, "wiki lint -p <项目名> --json")}
```

适合写入 wiki 的内容包括稳定构建命令、架构事实、巡检发现、常见失败、人工决策和项目约定。`wiki ingest` 只在显式调用时沉淀 trace/plan，并保留 source、created_at、related_task/session/workflow 元数据。不要写入 secret、API key、token、Feishu app_secret 或临时大段日志。

### 3.4 项目持久工作记忆

```bash
{_cmd(command, 'note add -p <项目名> "当前验证命令是 pytest tests"')}
{_cmd(command, 'note add -p <项目名> --priority "项目使用 Python 3.11"')}
{_cmd(command, "note show -p <项目名> --json")}
{_cmd(command, "note prune -p <项目名> --days 7")}
```

`note` 写入 `.codepilot/notepad.md`，适合记录跨会话仍要保留的短上下文。Priority Context 应保持短小；Working Memory 可裁剪；Manual 由人工维护。不要写入 secret、token 或密码。

### 3.5 查看活动时间线

```bash
{_cmd(command, "trace -p <项目名> --limit 30")}
{_cmd(command, "trace -p <项目名> --task <task_id> --json")}
```

`trace` 合并任务生命周期、任务日志、服务心跳和 workflow state，适合排查最近发生了什么、任务卡在哪个阶段、服务是否仍有心跳。

### 3.6 生成执行前需求规格

```bash
{_cmd(command, 'clarify -p <项目名> "改进 doctor" --json')}
```

`clarify` 只生成 `.codepilot/specs/clarify-*.md` 和 context artifact，写入 workflow state，不创建 backlog 任务、不启动执行器。适合先把模糊需求整理成目标、范围、非目标、约束、验收标准和待确认问题。

### 3.7 生成可审查执行计划

```bash
{_cmd(command, 'plan -p <项目名> "新增 explore" --use-wiki --json')}
{_cmd(command, "plan -p <项目名> --from-spec .codepilot/specs/example.md --json")}
```

`plan` 生成 `.codepilot/plans/plan-*.md` 和 context artifact，返回任务候选、wiki 引用、风险、执行顺序和验证矩阵。默认不创建 backlog、不启动执行器；人工确认后再导入任务或继续 clarify。

### 4. 精确查看单个任务

```bash
{_cmd(command, "task show <task_id>")}
{_cmd(command, "task show <task_id> --json")}
```

### 5. 环境自检

```bash
{_cmd(command, "doctor")}
{_cmd(command, "doctor --json")}
{_cmd(command, "doctor --fix --json")}
```

### 5.1 查看和测试事件 sink

```bash
{_cmd(command, "event schema --json")}
{_cmd(command, "event list -p <项目名> --json")}
{_cmd(command, "event test -p <项目名> --json")}
{_cmd(command, "hook validate -p <项目名> --json")}
{_cmd(command, "hook test -p <项目名> --provider codex --event agent.prompt.submitted --json")}
{_cmd(command, "hook logs -p <项目名> --json")}
{_cmd(command, "exec -p <项目名> --provider codex --dry-run --json -- codex --version")}
```

`hook` 和 `exec` 都只使用当前项目 `.codepilot/` 状态与日志；不会写 `.codex/hooks.json`，也不会修改 Claude/Gemini 的全局配置。

### 5.2 自我迭代 dry-run

```bash
{_cmd(command, 'self-update -p <项目名> --dry-run --json "改进目标"')}
{_cmd(command, 'self-update -p <项目名> --provider codex --provider gemini --dry-run "检查 provider 兼容性" --json')}
```

`self-update` 第一版只做项目内预检、证据采集和内存计划，输出后续人工可执行命令；不会创建 backlog、不会运行修复、不会写 wiki、不会提交代码。

### 5.3 查看技能目录

```bash
{_cmd(command, "skill list -p <项目名> --json")}
{_cmd(command, "skill search quality -p <项目名> --json")}
{_cmd(command, "skill enable build-fix -p <项目名> --json")}
{_cmd(command, 'skill run ralplan -p <项目名> --provider codex --input "新增 wiki context" --json')}
```

`skill` 管理 `.codepilot/skills/catalog.json` 中的本地技能元数据、启停状态和显式运行入口；当前不做远程安装，也不写用户 `$HOME/.codex/skills`。

### 6. 查看日志

```bash
{_cmd(command, "task logs <task_id>")}
{_cmd(command, "task logs <task_id> --tail 80")}
```

### 7. 停止任务

```bash
{_cmd(command, "task stop <task_id>")}
```

### 8. 手动重试任务

```bash
{_cmd(command, "task retry <task_id>")}
```

### 8.1 失败修复闭环

```bash
{_cmd(command, "build-fix -p <项目名> --json")}
{_cmd(command, 'build-fix -p <项目名> --task-id <task_id> --verify-command "pytest tests/test_x.py -q" --json')}
```

`build-fix` 会选择 failed 任务或指定任务，收集失败日志，重置为 backlog，调用现有执行器跑一轮，再执行验证命令并输出 `task_id`、`triage`、`actions`、`verification`、`verdict`。

### 9. 发布

最推荐：

```bash
{_cmd(command, "binary prepare --version 0.7.5")}
```

只打包：

```bash
{_cmd(command, "binary release --build-current")}
```

校验发布目录：

```bash
{_cmd(command, "binary verify")}
```

### 10. 图形界面

```bash
{_cmd(command, "ui")}
```

### 11. OpenCode 交互会话

`chat`、Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP。可以像使用 OpenCode 一样直接输入自然语言，OpenCode 会在会话里调用 CodePilot 的任务、状态、巡检、修复等工具。

```text
当前项目状态怎么样
优化飞书任务面板
重跑失败任务 12，先确认风险
```

### 12. 飞书与 Webhook

```bash
{_cmd(command, "feishu start")}
{_cmd(command, "feishu status")}
{_cmd(command, "feishu logs --tail 100")}
{_cmd(command, "feishu stop")}
{_cmd(command, "webhook --host 127.0.0.1 --port 8765")}
```

飞书通知优先使用 interactive 卡片或富文本 post；Webhook 飞书签名仍使用 `webhook_secret`。

## 结构化接口

### 命令清单 JSON

```bash
{_cmd(command, "ai manifest")}
```

### AI 手册 Markdown

```bash
{_cmd(command, "ai guide")}
```

### 给其他 AI 的短提示

```bash
{_cmd(command, "ai prompt")}
```

### 任务模板（外部规划专用）

如果你**不**走 CodePilot 的规划器，而是自己在外部规划好任务并通过 `add -f tasks.json` / `add -f tasks.md` 投递，
**必须按 task-template 格式准备 content，没有占位通道**。三种输出：

```bash
{_cmd(command, "ai template")}               # 原始 task-template.md（含 {{title}} 等占位符）
{_cmd(command, "ai template --format json")}  # 机器可读字段 schema + 批量导入格式
{_cmd(command, "ai template --format guide")} # 中文填充指南（含示例）
```

**强制规则（v0.2 起 add 命令的硬约束）**：

1. **人工调用方** —— 不要直接 `add`。要新增任务请走 `{command} "需求文本"`，由规划器拆分；要单独排一条具体任务也只是 `add -t "标题"`，由 `--agent` 指定的模型自动生成模板合规 content。
2. **AI / 智能体调用方** —— 必须满足下面之一：
   - 用 `add -f tasks.json`，每条带模板合规 `content`（缺章节直接拒）；
   - 用 `add -f tasks.md`，多个任务之间 `---` 分隔，每段都是完整 task-template；
   - 用 `add -t "标题"`，让 CodePilot 调用 AI 生成 content（同样会做合规校验）。
3. **`--no-ai` / `--allow-empty` 已废弃** —— 不再有空 content 的占位通道；老版本写入的占位任务 UI 上会提示按 `ai template --format json` schema 重新投递。
4. **章节骨架保留英文，章节正文用中文**；不要写「待补充」「TBD」「无」之类占位词。
"""


def ai_prompt_text(*, command_name: str = "codepilot", language: str = "en") -> str:
    """Return a compact prompt for another AI to operate CodePilot safely."""
    command = normalize_command_name(command_name)
    if normalize_agent_language(language) == "en":
        return (
            "You are calling CodePilot, a local engineering workflow CLI. Prefer non-interactive commands. "
            f"Submit requirements directly with `{command} \"requirement text\"`. "
            "chat, Web UI sessions, and Feishu free text enter OpenCode + CodePilot MCP and can receive questions, requirements, or operation intent directly. "
            "Treat project status, task counts, completion, failed tasks, running tasks, and service status as Q&A. "
            f"For failed-task repair loops, prefer `{_cmd(command, 'build-fix -p <project-name> --task-id <task_id> --json')}`. "
            "If you must submit tasks directly without the planner, provide complete task-template content; "
            f"read `{_cmd(command, 'ai template --format json')}` first. "
            "There is no --no-ai / --allow-empty placeholder path; missing sections are rejected. "
            f"For status use `{_cmd(command, 'status -p <project-name> --json')}`. "
            f"For one task use `{_cmd(command, 'task show <task_id> --json')}`. "
            f"For environment checks use `{_cmd(command, 'doctor --json')}`. "
            f"For troubleshooting use `{_cmd(command, 'task logs <task_id>')}`; stop tasks with `{_cmd(command, 'task stop <task_id>')}`; retry failed tasks with `{_cmd(command, 'task retry <task_id>')}`. "
            f"For a UI, start `{_cmd(command, 'ui')}`. "
            f"For release prep, prefer `{_cmd(command, 'binary prepare --version <version>')}`. "
            f"For the full command list, call `{_cmd(command, 'ai manifest')}`."
        )
    return (
        "你正在调用 CodePilot 这个本地 CLI。优先使用非交互命令。"
        f"提交需求时直接用 `{command} \"需求文本\"`。"
        "chat、Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP，可以直接表达问题、需求或操作意图；"
        "项目状态、任务数量、完成度、失败任务、运行中任务和服务状态问题应作为问答处理。"
        f"失败任务需要修复闭环时优先用 `{_cmd(command, 'build-fix -p <项目名> --task-id <task_id> --json')}`。"
        "如果你必须自己写任务（不走规划器），必须按 task-template 提供完整 content，"
        f"先用 `{_cmd(command, 'ai template --format json')}` 拿 schema 再投递；"
        "没有 --no-ai / --allow-empty 这种占位通道，缺章节直接拒。"
        f"查看状态时优先用 `{_cmd(command, 'status -p <项目名> --json')}`，"
        f"精确查看单个任务用 `{_cmd(command, 'task show <task_id> --json')}`，"
        f"检查本机环境用 `{_cmd(command, 'doctor --json')}`，"
        f"排障时用 `{_cmd(command, 'task logs <task_id>')}`，停止任务用 `{_cmd(command, 'task stop <task_id>')}`，"
        f"重试失败任务用 `{_cmd(command, 'task retry <task_id>')}`。"
        f"如果需要人工介入或图形化查看，启动 `{_cmd(command, 'ui')}`。"
        f"准备发布包时优先用 `{_cmd(command, 'binary prepare --version <版本号>')}`。"
        f"如果需要完整命令清单，调用 `{_cmd(command, 'ai manifest')}`。"
    )
