"""Machine-readable CodePilot command manifest."""

from __future__ import annotations

import json
from typing import Any

from codepilot import __version__
from codepilot.ai_support.agent_commands import _cmd, normalize_command_name
from codepilot.ai_support.project_metadata import project_metadata
from codepilot.core.config import normalize_agent_language

def command_manifest(
    *,
    version: str | None = None,
    command_name: str = "codepilot",
    binary_name: str = "codepilot",
    language: str = "en",
) -> dict[str, Any]:
    """Return a machine-readable manifest for other AI tools."""
    command = normalize_command_name(command_name)
    manifest_version = (version or __version__).strip()
    binary = normalize_command_name(binary_name)
    lang = normalize_agent_language(language)
    if lang == "en":
        return _english_command_manifest(command=command, version=manifest_version, binary=binary)
    return {
        "name": "CodePilot",
        "version": manifest_version,
        **project_metadata(),
        "command_name": command,
        "language": lang,
        "description": (
            "Local engineering workflow CLI that turns natural-language requirements into tasks and coordinates planning, execution, review, operations, and release."
            if lang == "en"
            else "本地工程工作流 CLI，可把自然语言需求转换成任务，并自动规划、执行、审查和发布。"
        ),
            "calling_principles": [
                "优先使用非交互命令，避免 chat 模式，除非明确需要持续会话。",
                "需要结构化结果时，优先使用 `hud --json`、`status --json`、`task show --json`、`doctor --json`、`task find --json`、`ai manifest`。",
                f"如果目标是提交一个自然语言需求，直接调用 `{command} \"需求文本\"` 或 `{command} go \"需求文本\"`。",
                "chat、Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP；自由文本可以直接表达问题、需求或操作意图。",
                "需要确定性 artifact 时显式调用 `clarify` / `plan` 或 MCP 工具。",
                "clarify/plan 产生的 `next_actions` 应通过 `workflow next --list` / `workflow next --action <id>` 推进；`suggested_command` 只用于展示/审查。",
                "如果用户是在询问项目、任务数量、完成度、失败任务、运行中任务或服务状态，优先按 `question` 处理，CodePilot 会读取本地项目与任务数据辅助回答。",
                f"如果目标是发布产物，优先调用 `{_cmd(command, 'binary prepare --version <版本号>')}`。",
                f"如果任务处于运行中，先用 `{_cmd(command, 'status -p <项目名> -v')}` 查看阶段，再决定是否 `task logs` 或 `task stop`。",
                f"如果任务失败且需要完整修复闭环，优先用 `{_cmd(command, 'build-fix -p <项目名> --task-id <task_id> --json')}`；只需重新排队时再用 `{_cmd(command, 'task retry <task_id>')}`。",
            ],
        "structured_outputs": [
            {
                "command": _cmd(command, "ai manifest"),
                "format": "json",
                "purpose": "输出完整命令清单、常见工作流和调用建议。",
            },
            {
                "command": _cmd(command, "setup . --dry-run --json"),
                "format": "json",
                "purpose": "预检项目级 .codepilot 初始化、配置和项目注册动作，不写入真实 hooks。",
            },
            {
                "command": _cmd(command, 'self-update -p <项目名> --dry-run --json "改进目标"'),
                "format": "json",
                "purpose": "项目内自我迭代 dry-run：只做预检、证据采集和内存计划，不创建任务、不执行修复、不提交代码。",
            },
            {
                "command": _cmd(command, "status -p <项目名> --json"),
                "format": "json",
                "purpose": "获取项目任务状态和任务列表。",
            },
            {
                "command": _cmd(command, "hud -p <项目名> --preset full --json"),
                "format": "json",
                "purpose": "获取轻量工作流 HUD：项目队列、运行任务、最近活动和服务状态。",
            },
            {
                "command": _cmd(command, "explore --prompt <问题> --json"),
                "format": "json",
                "purpose": "只读探索项目文件、Git、任务日志和 inspect 信号，返回 evidence/sources/limitations。",
            },
            {
                "command": _cmd(command, "plan -p <项目名> <需求> --json"),
                "format": "json",
                "purpose": "生成可审查执行计划 artifact、任务候选、风险和验证矩阵；默认不创建 backlog。",
            },
            {
                "command": _cmd(command, "workflow status -p <项目名> --json"),
                "format": "json",
                "purpose": "读取当前 workflow 状态、mode 状态和 agent session，定位 artifact 与 next_actions 来源。",
            },
            {
                "command": _cmd(command, "workflow next -p <项目名> --list --json"),
                "format": "json",
                "purpose": "列出当前 artifact/workflow 的 next_actions，供外部 Agent 选择安全推进动作。",
            },
            {
                "command": _cmd(command, "workflow next -p <项目名> --action <id> --json"),
                "format": "json",
                "purpose": "通过固定 allowlist 执行指定 next_action；不会执行 suggested_command 字符串。",
            },
            {
                "command": _cmd(command, "wiki list -p <项目名> --json"),
                "format": "json",
                "purpose": "列出项目本地 Markdown wiki 页面。",
            },
            {
                "command": _cmd(command, "wiki query <关键词> -p <项目名> --json"),
                "format": "json",
                "purpose": "检索项目本地 wiki 中沉淀的构建命令、架构事实、失败模式和人工决策。",
            },
            {
                "command": _cmd(command, "wiki ingest --from trace -p <项目名> --json"),
                "format": "json",
                "purpose": "显式把 trace 时间线沉淀为 wiki 页面，保留来源和关联任务/工作流元数据。",
            },
            {
                "command": _cmd(command, "note show -p <项目名> --json"),
                "format": "json",
                "purpose": "读取项目持久工作记忆，恢复跨会话关键上下文。",
            },
            {
                "command": _cmd(command, "trace -p <项目名> --json"),
                "format": "json",
                "purpose": "获取项目最近活动时间线，合并任务、日志、服务心跳和 workflow state。",
            },
            {
                "command": _cmd(command, "task find <关键词> -p <项目名> --json"),
                "format": "json",
                "purpose": "搜索任务并获取结构化结果。",
            },
            {
                "command": _cmd(command, "task show <task_id> --json"),
                "format": "json",
                "purpose": "精确获取单个任务的完整详情和历史日志记录。",
            },
            {
                "command": _cmd(command, "doctor --json"),
                "format": "json",
                "purpose": "检查本机运行环境、CLI 工具、配置和数据库可用性。",
            },
            {
                "command": _cmd(command, "doctor --fix --json"),
                "format": "json",
                "purpose": "执行保守项目级自动修复：运行 setup 准备配置和 .codepilot 目录，不修改真实 hooks。",
            },
            {
                "command": _cmd(command, "event list -p <项目名> --json"),
                "format": "json",
                "purpose": "列出项目本地事件 sink registry，用于 Hook/Event 插件化集成。",
            },
            {
                "command": _cmd(command, "event schema --json"),
                "format": "json",
                "purpose": "列出 CodePilot 事件类型、必需字段和 payload 字段约定。",
            },
            {
                "command": _cmd(command, "hook validate -p <项目名> --json"),
                "format": "json",
                "purpose": "验证项目级 hook registry 和 provider-neutral lifecycle 事件，不写全局 hooks。",
            },
            {
                "command": _cmd(command, "exec --provider codex --dry-run --json -- codex --version"),
                "format": "json",
                "purpose": "在当前项目内对 codex/claude/gemini/custom 做 provider preflight 或命令烟测，不安装 wrapper。",
            },
            {
                "command": _cmd(command, "build-fix -p <项目名> --json"),
                "format": "json",
                "purpose": "对 failed 任务执行收集失败、重试修复、验证和 verdict 输出的质量闭环。",
            },
            {
                "command": _cmd(command, "skill list -p <项目名> --json"),
                "format": "json",
                "purpose": "列出项目本地 skill catalog，查看内置/项目技能启用状态。",
            },
            {
                "command": _cmd(command, 'skill run ralplan -p <项目名> --provider codex --input "需求" --json'),
                "format": "json",
                "purpose": "运行已启用的项目本地技能，复用 CodePilot 现有 clarify/plan/wiki/build-fix 能力。",
            },
            {
                "command": _cmd(command, "ai template --format json"),
                "format": "json",
                "purpose": "获取任务模板字段 schema 与批量导入格式，用于外部 AI 规划后投递任务。",
            },
            {
                "command": _cmd(command, "feishu handle-event"),
                "format": "json",
                "purpose": "飞书长连接 worker 内部入口：从 stdin 读取事件 payload，并输出干净 JSON 回复。",
            },
        ],
        "commands": [
            {
                "name": "init",
                "syntax": _cmd(command, "init <path>"),
                "purpose": "初始化项目并生成 AGENTS.toml。",
                "when_to_use": "第一次接入某个仓库时。",
                "examples": [_cmd(command, "init ."), _cmd(command, "init D:\\repo -n demo")],
            },
            {
                "name": "setup",
                "syntax": _cmd(command, "setup [path] [--name <项目名>] [--dry-run] [--json]"),
                "purpose": "初始化项目级 .codepilot 目录骨架、同步 AGENTS.toml 和项目注册记录；第一阶段不会修改 .codex/hooks.json。",
                "when_to_use": "希望按项目准备 CodePilot 状态目录和配置，或在安装 hooks 前先做 dry-run 预检时。",
                "examples": [
                    _cmd(command, "setup ."),
                    _cmd(command, "setup . --dry-run --json"),
                    _cmd(command, "setup D:\\repo -n demo"),
                ],
            },
            {
                "name": "self_update",
                "syntax": _cmd(command, 'self-update -p <项目名> --dry-run [--provider codex] "改进目标" [--json]'),
                "purpose": "项目内自我迭代 dry-run：复用 doctor、hook、event、exec、trace、explore、wiki 和 plan 生成下一步升级计划；不自动改代码。",
                "when_to_use": "想让 CodePilot 先审计自身状态、收集证据并给出下一轮升级方案，但还不希望创建任务、执行修复或提交时。",
                "examples": [
                    _cmd(command, 'self-update -p codepilot-dev --dry-run --json "改进多智能体兼容性"'),
                    _cmd(command, 'self-update -p codepilot-dev --provider codex --provider gemini --dry-run "检查 provider 兼容性" --json'),
                ],
            },
            {
                "name": "goal",
                "syntax": f'{command} "需求文本"',
                "purpose": "提交一个自然语言需求，让 CodePilot 自动判断是否拆分并按配置执行。",
                "when_to_use": "其他 AI 想把高层目标交给 CodePilot 自己执行时。",
                "examples": [f'{command} "修复任务重试逻辑并补测试"'],
            },
            {
                "name": "go",
                "syntax": _cmd(command, "go <需求或问题> [--project <项目名>]"),
                "purpose": "显式自然语言入口：可回答问题，也可把明确需求/任务进入规划或执行。",
                "when_to_use": "需要避免被 shell 顶层命令解析影响，或希望显式传入项目、规划器、执行器选项时。",
                "examples": [
                    _cmd(command, 'go "当前项目有多少任务，完成了多少" -p codepilot-dev'),
                    _cmd(command, 'go "修复任务重试逻辑并补测试" -p codepilot-dev'),
                ],
            },
            {
                "name": "chat",
                "syntax": _cmd(command, "chat [-p <项目名>] [-a opencode]"),
                "purpose": "启动 CodePilot 管理的 OpenCode TUI，并注入隔离配置、CodePilot MCP、agent、commands、provider/model 和权限配置。",
                "when_to_use": "需要持续会话、交互式查看任务状态、提交需求或执行项目工作流时。",
                "examples": [
                    _cmd(command, "chat -p codepilot-dev -a opencode"),
                    "在 OpenCode 中输入：优化飞书任务面板",
                    "在 OpenCode 中输入：当前有多少任务，完成了多少",
                ],
            },
            {
                "name": "status",
                "syntax": _cmd(command, "status -p <项目名> [-v|--json]"),
                "purpose": "查看任务总览、运行态、最后输出。",
                "when_to_use": "检查是否还有 backlog、是否有任务卡住、当前运行到哪一阶段。",
                "examples": [
                    _cmd(command, "status -p codepilot-dev -v"),
                    _cmd(command, "status -p codepilot-dev --json"),
                ],
            },
            {
                "name": "hud",
                "syntax": _cmd(command, "hud [-p <项目名>] [--all] [--preset minimal|focused|full] [--watch|--json]"),
                "purpose": "显示轻量工作流 HUD，汇总项目队列、运行中任务、最近活动和后台服务状态。",
                "when_to_use": "需要快速判断当前工作台是否繁忙、是否有失败任务、服务是否仍有心跳时。",
                "examples": [
                    _cmd(command, "hud -p codepilot-dev --preset full"),
                    _cmd(command, "hud -p codepilot-dev --preset full --json"),
                    _cmd(command, "hud --watch"),
                ],
            },
            {
                "name": "logs",
                "syntax": _cmd(command, "task logs <task_id> [--tail N|--full]"),
                "purpose": "查看任务实时日志或历史日志。",
                "when_to_use": "需要了解任务执行细节或错误上下文。",
                "examples": [_cmd(command, "task logs 7"), _cmd(command, "task logs 7 --tail 50")],
            },
            {
                "name": "show",
                "syntax": _cmd(command, "task show <task_id> [--logs|--json]"),
                "purpose": "精确查看单个任务的完整元数据、任务内容、错误信息、交付记录和日志记录摘要。",
                "when_to_use": "已经知道任务 ID，需要完整任务详情而不是状态列表摘要时。",
                "examples": [_cmd(command, "task show 7"), _cmd(command, "task show 7 --json")],
            },
            {
                "name": "doctor",
                "syntax": _cmd(command, "doctor [--project <项目名>] [--services] [--fix] [--json]"),
                "purpose": "检查 CodePilot 当前运行环境、配置、CLI 工具、API Key、任务数据库和项目服务；--fix 会执行保守项目级 setup；有项目上下文时会发布 doctor.checked 事件到启用的 sink。",
                "when_to_use": "接入新机器、排查环境问题、需要机器可读环境健康状态，或需要补齐 .codepilot 项目骨架时。",
                "examples": [_cmd(command, "doctor"), _cmd(command, "doctor --json"), _cmd(command, "doctor --fix --json")],
            },
            {
                "name": "event",
                "syntax": _cmd(command, "event <schema|list|register|enable|disable|test> [--project <项目名>] [--json]"),
                "purpose": "管理项目本地事件 sink registry 和事件 schema；当前支持 JSONL sink，用于后续 hook/event 插件订阅。",
                "when_to_use": "需要查看、注册或验证项目级事件输出插件时。",
                "examples": [
                    _cmd(command, "event schema --json"),
                    _cmd(command, "event list -p codepilot-dev --json"),
                    _cmd(command, "event register -p codepilot-dev --name audit --type jsonl --path .codepilot/events/audit.jsonl --event doctor.checked"),
                    _cmd(command, "event enable -p codepilot-dev audit --json"),
                    _cmd(command, "event test -p codepilot-dev --event doctor.checked --json"),
                ],
            },
            {
                "name": "hook",
                "syntax": _cmd(command, "hook <plan|validate|test|logs|install|uninstall> -p <项目名> [--json]"),
                "purpose": "管理项目级 hook registry，并用 provider-neutral lifecycle 事件做本地验证和日志读取；不写真实 Codex/Claude/Gemini 全局配置。",
                "when_to_use": "需要确认 hook/event 插件化集成是否可用，或需要生成 agent lifecycle 测试事件时。",
                "examples": [
                    _cmd(command, "hook validate -p codepilot-dev --json"),
                    _cmd(command, "hook test -p codepilot-dev --provider codex --event agent.prompt.submitted --json"),
                    _cmd(command, "hook logs -p codepilot-dev --json"),
                ],
            },
            {
                "name": "exec",
                "syntax": _cmd(command, "exec -p <项目名> --provider codex|claude|gemini|custom [--dry-run] [--json] -- <command...>"),
                "purpose": "项目内 provider-neutral 命令烟测和审计执行，记录到 `.codepilot/exec` 并投递 exec 事件；不安装 shell alias、不替换原生命令。",
                "when_to_use": "需要确认 Codex、Claude、Gemini 或自定义命令在当前项目能否被调用，或需要留下项目内审计记录时。",
                "examples": [
                    _cmd(command, "exec -p codepilot-dev --provider codex --dry-run --json -- codex --version"),
                    _cmd(command, 'exec -p codepilot-dev --provider custom --json -- python -c "print(123)"'),
                ],
            },
            {
                "name": "stop",
                "syntax": _cmd(command, "task stop <task_id> [-m 原因]"),
                "purpose": "停止运行中的任务。",
                "when_to_use": "任务卡住、执行方向错误、需要强制终止时。",
                "examples": [_cmd(command, "task stop 7"), _cmd(command, 'task stop 7 -m "方向错误，停止重跑"')],
            },
            {
                "name": "retry",
                "syntax": _cmd(command, "task retry <task_id>"),
                "purpose": "手动重试指定任务，重置运行态并重新放回 backlog。",
                "when_to_use": "任务 failed/cancelled 后需要人工重新触发时。",
                "examples": [_cmd(command, "task retry 7")],
            },
            {
                "name": "run",
                "syntax": _cmd(command, "run -p <项目名> [--executor builtin|dispatch]"),
                "purpose": "执行 backlog 中的任务。",
                "when_to_use": "已存在 backlog，想手动触发执行。",
                "examples": [_cmd(command, "run -p codepilot-dev --executor builtin --no-auto-commit")],
            },
            {
                "name": "build_fix",
                "syntax": _cmd(command, "build-fix -p <项目名> [--task-id <id>] [--verify-command <cmd>] [--json]"),
                "purpose": "对 failed 任务执行质量闭环：收集失败日志、重置重试、调用执行器、运行验证命令并输出 verdict。",
                "when_to_use": "失败任务需要从失败原因到修复验证的一键入口，而不是单纯 `task retry` 时。",
                "examples": [
                    _cmd(command, "build-fix -p codepilot-dev --json"),
                    _cmd(command, 'build-fix -p codepilot-dev --task-id 7 --verify-command "pytest tests/test_x.py -q" --json'),
                ],
            },
            {
                "name": "task_find",
                "syntax": _cmd(command, "task find <关键词> -p <项目名> [--json]"),
                "purpose": "按关键词、状态、优先级检索任务。",
                "when_to_use": "需要快速定位任务或批量筛选目标任务时。",
                "examples": [
                    _cmd(command, "task find retry -p codepilot-dev"),
                    _cmd(command, "task find bug -p codepilot-dev --json"),
                ],
            },
            {
                "name": "ui",
                "syntax": (
                    f"{_cmd(command, 'ui [--host 127.0.0.1] [--port 8766]')} | "
                    f"{_cmd(command, 'ui <start|status|logs|stop|restart>')}"
                ),
                "purpose": "统一 Web UI 入口：支持前台启动，也支持后台服务管理。",
                "when_to_use": "需要图形化总览多个项目，或需要让 Web UI 后台持续运行与排障时。",
                "examples": [
                    _cmd(command, "ui"),
                    _cmd(command, "ui --no-open --port 8877"),
                    _cmd(command, "ui start"),
                    _cmd(command, "ui logs --tail 100"),
                ],
            },
            {
                "name": "daemon",
                "syntax": _cmd(command, "daemon -p <项目名> [--status|--stop]"),
                "purpose": "按项目后台轮询 backlog 并持续执行任务。",
                "when_to_use": "希望项目任务持续自动执行，而不是手动反复运行 run 时。",
                "examples": [
                    _cmd(command, "daemon -p codepilot-dev"),
                    _cmd(command, "daemon -p codepilot-dev --status"),
                    _cmd(command, "daemon -p codepilot-dev --stop"),
                ],
            },
            {
                "name": "inspect",
                "syntax": _cmd(command, "inspect -p <项目名> [--once|--status|--stop|--json]"),
                "purpose": "巡检项目信号并产出候选任务。",
                "when_to_use": "需要持续发现技术债、失败任务、待优化点时。",
                "examples": [
                    _cmd(command, "inspect -p codepilot-dev --once"),
                    _cmd(command, "inspect -p codepilot-dev --status"),
                ],
            },
            {
                "name": "explore",
                "syntax": _cmd(command, "explore --prompt <问题> [-p <项目名>] [--use-wiki|--no-wiki] [--json]"),
                "purpose": "只读查询项目文件、Git 状态、任务日志摘要、wiki 上下文和 inspect 信号，输出可复用证据。",
                "when_to_use": "澄清或规划前需要本地证据，但不应修改文件、启动服务、安装依赖或执行测试时。",
                "examples": [
                    _cmd(command, 'explore --prompt "find task template" --json'),
                    _cmd(command, 'explore -p codepilot-dev "recent failed task logs"'),
                ],
            },
            {
                "name": "clarify",
                "syntax": _cmd(command, "clarify [-p <项目名>] [--quick|--standard] <需求> [--json]"),
                "purpose": "生成执行前需求规格 artifact，明确目标、范围、非目标、约束、验收标准和待确认问题。",
                "when_to_use": "需求仍模糊但还不应创建任务或执行代码时；输出 spec 后再进入 plan/go。",
                "examples": [
                    _cmd(command, 'clarify -p codepilot-dev "改进 doctor" --json'),
                    _cmd(command, 'clarify --quick -p codepilot-dev "优化任务面板"'),
                ],
            },
            {
                "name": "plan",
                "syntax": _cmd(command, "plan [-p <项目名>] <需求> [--from-spec <path>] [--use-wiki|--no-wiki] [--json]"),
                "purpose": "生成可审查执行计划 artifact，输出执行顺序、文件范围、wiki 引用、风险、验证矩阵和任务候选。",
                "when_to_use": "需求已足够进入计划审查，但还不应创建 backlog 或执行代码时；可消费 clarify spec。",
                "examples": [
                    _cmd(command, 'plan -p codepilot-dev "新增 explore" --json'),
                    _cmd(command, "plan -p codepilot-dev --from-spec .codepilot/specs/example.md --json"),
                ],
            },
            {
                "name": "workflow_status",
                "syntax": _cmd(command, "workflow status -p <项目名> [--mode <mode>] --json"),
                "purpose": "查看 workflow mode 状态和 agent session，包含 artifact 路径、上下文和 next_actions 来源。",
                "when_to_use": "外部 Agent 需要读取 clarify/plan 产物状态，或在执行下一步前确认当前 workflow 上下文时。",
                "examples": [
                    _cmd(command, "workflow status -p codepilot-dev --json"),
                    _cmd(command, "workflow status -p codepilot-dev --mode plan --json"),
                ],
            },
            {
                "name": "workflow_next",
                "syntax": _cmd(command, "workflow next -p <项目名> [--mode <mode>] [--list | --action <id>] [--allow-high-risk] --json"),
                "purpose": "列出或安全执行 artifact next_actions；动作必须在固定 allowlist 内，且不会执行 suggested_command 字符串。",
                "when_to_use": "clarify/plan 输出 next_actions 后，需要由外部 Agent 以受控方式继续生成计划或导入任务时。",
                "examples": [
                    _cmd(command, "workflow next -p codepilot-dev --list --json"),
                    _cmd(command, "workflow next -p codepilot-dev --action plan_from_spec --json"),
                    _cmd(command, "workflow next -p codepilot-dev --action import_tasks --json"),
                ],
            },
            {
                "name": "wiki",
                "syntax": _cmd(command, "wiki <add|list|query|update|delete|refresh|lint|ingest> ..."),
                "purpose": "维护项目本地 Markdown 知识库，沉淀长期有用的项目事实；ingest 可显式沉淀 trace/plan artifact。",
                "when_to_use": "需要记录或复用构建命令、架构事实、巡检发现、常见失败、trace/plan 结果和人工决策时；不要写入 secret。",
                "examples": [
                    _cmd(command, 'wiki add -p codepilot-dev --title "构建命令" --body "pytest tests"'),
                    _cmd(command, 'wiki query -p codepilot-dev "构建" --json'),
                    _cmd(command, 'wiki update -p codepilot-dev --slug build --body "pytest -q" --json'),
                    _cmd(command, "wiki ingest --from plan -p codepilot-dev --json"),
                    _cmd(command, "wiki lint -p codepilot-dev --json"),
                ],
            },
            {
                "name": "skill",
                "syntax": _cmd(command, "skill <list|search|show|enable|disable|run> -p <项目名> [--json]"),
                "purpose": "管理并运行项目本地 skill catalog，当前只复用 CodePilot 已有能力，不做远程安装。",
                "when_to_use": "需要浏览可用工作流技能、开启本地技能标记，或通过 skill run 显式调用 clarify/plan/wiki/build-fix 能力时。",
                "examples": [
                    _cmd(command, "skill list -p codepilot-dev --json"),
                    _cmd(command, "skill search quality -p codepilot-dev --json"),
                    _cmd(command, "skill enable build-fix -p codepilot-dev --json"),
                    _cmd(command, 'skill run ralplan -p codepilot-dev --provider codex --input "新增 wiki context" --json'),
                ],
            },
            {
                "name": "note",
                "syntax": _cmd(command, "note <add|show|prune|clear> ..."),
                "purpose": "维护项目持久工作记忆 `.codepilot/notepad.md`，保存跨会话关键上下文。",
                "when_to_use": "需要记录当前任务的短期上下文、人工约束、后续必须记住的事实，避免上下文压缩或换会话后丢失时。",
                "examples": [
                    _cmd(command, 'note add -p codepilot-dev "pytest tests 是当前主验证命令"'),
                    _cmd(command, 'note add -p codepilot-dev --priority "项目使用 Python 3.11"'),
                    _cmd(command, "note show -p codepilot-dev --json"),
                    _cmd(command, "note prune -p codepilot-dev --days 7"),
                ],
            },
            {
                "name": "trace",
                "syntax": _cmd(command, "trace [-p <项目名>] [--task <task_id>] [--limit N] [--json]"),
                "purpose": "显示项目最近活动时间线，合并任务生命周期、任务日志、服务心跳和 workflow state。",
                "when_to_use": "需要排查最近发生了什么、任务卡在哪个阶段、服务是否仍有心跳，或向其他 Agent 提供时间线证据时。",
                "examples": [
                    _cmd(command, "trace -p codepilot-dev --limit 30"),
                    _cmd(command, "trace -p codepilot-dev --task 7 --json"),
                ],
            },
            {
                "name": "webhook",
                "syntax": _cmd(command, "webhook [--host 127.0.0.1] [--port 8765]"),
                "purpose": "启动轻量 HTTP Webhook 服务，接收外部系统任务投递；任务状态通知支持 Feishu interactive 卡片、企业微信和 generic JSON。",
                "when_to_use": "外部系统需要通过 POST /tasks 写入 backlog，或项目配置了通知 webhook 时。",
                "examples": [_cmd(command, "webhook --host 127.0.0.1 --port 8765")],
            },
            {
                "name": "feishu",
                "syntax": _cmd(command, "feishu <start|status|logs|stop|run|handle-event>"),
                "purpose": "管理飞书企业应用长连接机器人，支持项目选择、状态/任务卡片、需求会话、任务控制和富文本回复。",
                "when_to_use": "希望在飞书中查看项目状态、问答、提交显式需求、控制任务或接收通知时。",
                "examples": [
                    _cmd(command, "feishu start"),
                    _cmd(command, "feishu status"),
                    _cmd(command, "feishu logs --tail 100"),
                    _cmd(command, "feishu stop"),
                ],
            },
            {
                "name": "binary_prepare",
                "syntax": _cmd(command, "binary prepare --version <版本号>"),
                "purpose": "更新版本号、构建当前平台、生成发布目录并自动校验。",
                "when_to_use": "准备一个可交付的本地发布包时。",
                "examples": [_cmd(command, "binary prepare --version 0.7.5")],
            },
            {
                "name": "binary_release",
                "syntax": _cmd(command, "binary release [--build-current] [--artifact 平台=路径]"),
                "purpose": "把已有二进制整理成标准发布目录。",
                "when_to_use": "已有一个或多个平台二进制，准备打包发布时。",
                "examples": [
                    _cmd(command, "binary release --build-current"),
                    _cmd(command, f"binary release --artifact linux-x86_64=dist/binary/linux-x86_64/{binary}"),
                ],
            },
            {
                "name": "binary_verify",
                "syntax": _cmd(command, "binary verify [--release-dir 发布目录]"),
                "purpose": "校验发布目录、压缩包内容和校验值。",
                "when_to_use": "发布前自检或 CI 验证时。",
                "examples": [
                    _cmd(command, "binary verify"),
                    _cmd(command, "binary verify --release-dir dist/release/codepilot-0.7.4-summary"),
                ],
            },
            {
                "name": "ai_template",
                "syntax": _cmd(command, "ai template [--format md|json|guide]"),
                "purpose": "输出任务模板：md=原始模板，json=字段 schema，guide=中文填充指南。外部 AI / Web 批量添加任务时必须遵循本模板。",
                "when_to_use": "外部 AI 自己规划了任务，想直接 `add -f tasks.json` 投递，先读取本命令了解字段规范与批量 JSON 格式。",
                "examples": [
                    _cmd(command, "ai template"),
                    _cmd(command, "ai template --format json"),
                    _cmd(command, "ai template --format guide"),
                ],
            },
        ],
        "workflows": [
            {
                "name": "提交一个新需求并执行",
                "steps": [
                    _cmd(command, "setup ."),
                    f'{command} "实现一个需求"',
                    _cmd(command, "status -p <项目名> -v"),
                    _cmd(command, "task show <task_id>"),
                    _cmd(command, "task logs <task_id>"),
                ],
            },
            {
                "name": "在会话或飞书中使用 OpenCode 工作流",
                "steps": [
                    "普通提问：直接输入 `当前有多少任务，完成了多少`。",
                    "提交需求：直接输入自然语言需求，OpenCode 会通过 CodePilot MCP 调用任务和规划能力。",
                    "任务操作：可以输入 `retry 123`、`tasks failed` 等明确命令，也可以用自然语言说明操作意图。",
                    "需要规格或计划 artifact 时显式调用 `clarify` / `plan`。",
                    "CodePilot 启动的 OpenCode 使用 `~/.codepilot/opencode/<项目标识>/` 隔离运行时，保存生成配置、会话数据和项目级模型选择。",
                    "没有当前项目的飞书自由文本会先返回项目选择卡片。",
                ],
            },
            {
                "name": "审查 artifact 并安全推进 next_action",
                "steps": [
                    _cmd(command, "workflow status -p <项目名> --json"),
                    _cmd(command, "workflow next -p <项目名> --list --json"),
                    "# 只选择已展示的 action id；不要拼接执行 suggested_command",
                    _cmd(command, "workflow next -p <项目名> --action <id> --json"),
                ],
            },
            {
                "name": "排障运行中的任务",
                "steps": [
                    _cmd(command, "status -p <项目名> -v"),
                    _cmd(command, "task show <task_id>"),
                    _cmd(command, "task logs <task_id> --tail 80"),
                    _cmd(command, "task stop <task_id>"),
                ],
            },
            {
                "name": "重试失败或取消的任务",
                "steps": [
                    _cmd(command, "task logs <task_id> --tail 80"),
                    _cmd(command, "task retry <task_id>"),
                    _cmd(command, "run -p <项目名>"),
                ],
            },
            {
                "name": "准备一个发布包",
                "steps": [
                    _cmd(command, "binary prepare --version <版本号>"),
                    _cmd(command, "binary verify"),
                ],
            },
            {
                "name": "外部 AI 自行规划后投递任务（不走 CodePilot 规划器）",
                "steps": [
                    _cmd(command, "ai template --format json"),
                    "# 按 schema 渲染出 tasks.json，每项 content 字段填写符合模板的中文正文",
                    _cmd(command, "add -p <项目名> -f tasks.json"),
                    _cmd(command, "status -p <项目名> -v"),
                ],
            },
        ],
    }


def _english_command_manifest(*, command: str, version: str, binary: str) -> dict[str, Any]:
    project = "<project-name>"
    return {
        "name": "CodePilot",
        "version": version,
        **project_metadata(),
        "command_name": command,
        "language": "en",
        "description": "Local engineering workflow CLI that turns natural-language requirements into tasks and coordinates planning, execution, review, operations, and release.",
        "calling_principles": [
            "Prefer non-interactive commands and avoid chat mode unless a persistent conversation is explicitly needed.",
            "When structured output is needed, prefer `hud --json`, `status --json`, `task show --json`, `doctor --json`, `task find --json`, or `ai manifest`.",
            f"To submit a natural-language requirement, call `{command} \"requirement text\"` or `{command} go \"requirement text\"`.",
            "Chat, Web UI sessions, and Feishu free text all route through OpenCode + CodePilot MCP; free text may express a question, requirement, or operation intent.",
            "Call `clarify` / `plan` or MCP tools explicitly when deterministic artifacts are required.",
            "Advance `clarify` / `plan` `next_actions` with `workflow next --list` / `workflow next --action <id>`; treat `suggested_command` as display/review metadata only.",
            "If the user asks about project state, task counts, completion ratio, failed tasks, running tasks, or service status, treat it as a question and let CodePilot read local task/project data.",
            f"For release artifacts, prefer `{_cmd(command, 'binary prepare --version <version>')}`.",
            f"If a task is running, inspect its phase with `{_cmd(command, f'status -p {project} -v')}` before deciding whether to view logs or stop it.",
            f"If a failed task needs a full repair loop, prefer `{_cmd(command, f'build-fix -p {project} --task-id <task_id> --json')}`; use `{_cmd(command, 'task retry <task_id>')}` only to requeue it.",
        ],
        "structured_outputs": [
            {"command": _cmd(command, "ai manifest"), "format": "json", "purpose": "Return this machine-readable command manifest."},
            {"command": _cmd(command, "ai guide"), "format": "markdown", "purpose": "Return the AI usage guide."},
            {"command": _cmd(command, "ai template --format json"), "format": "json", "purpose": "Return task-template fields and batch-import schema."},
            {"command": _cmd(command, "setup . --dry-run --json"), "format": "json", "purpose": "Preview project setup actions without writing hooks."},
            {"command": _cmd(command, f'self-update -p {project} --dry-run --json "improvement goal"'), "format": "json", "purpose": "Run self-improvement preflight and planning without creating tasks or changing code."},
            {"command": _cmd(command, f"status -p {project} --json"), "format": "json", "purpose": "Return project task status and task lists."},
            {"command": _cmd(command, f"hud -p {project} --preset full --json"), "format": "json", "purpose": "Return queue, running task, recent activity, and service status."},
            {"command": _cmd(command, "explore --prompt <question> --json"), "format": "json", "purpose": "Read-only project exploration with evidence, sources, and limitations."},
            {"command": _cmd(command, f"plan -p {project} <requirement> --json"), "format": "json", "purpose": "Create a reviewable plan artifact with risks, verification matrix, and task candidates."},
            {"command": _cmd(command, f"workflow status -p {project} --json"), "format": "json", "purpose": "Return workflow state, agent session, artifact paths, and next-action context."},
            {"command": _cmd(command, f"workflow next -p {project} --list --json"), "format": "json", "purpose": "List the current workflow next_actions for safe external-agent selection."},
            {"command": _cmd(command, f"workflow next -p {project} --action <id> --json"), "format": "json", "purpose": "Execute one allowlisted next_action without shelling out to suggested_command."},
            {"command": _cmd(command, f"task find <keyword> -p {project} --json"), "format": "json", "purpose": "Search tasks and return structured matches."},
            {"command": _cmd(command, "task show <task_id> --json"), "format": "json", "purpose": "Return one task's full metadata, content, errors, delivery record, and log summary."},
            {"command": _cmd(command, "doctor --json"), "format": "json", "purpose": "Check local environment, CLI tools, configuration, and database health."},
            {"command": _cmd(command, "event schema --json"), "format": "json", "purpose": "Return CodePilot event types and payload-field contracts."},
            {"command": _cmd(command, f"hook validate -p {project} --json"), "format": "json", "purpose": "Validate project hook registry and provider-neutral lifecycle events."},
            {"command": _cmd(command, "exec --provider codex --dry-run --json -- codex --version"), "format": "json", "purpose": "Run provider preflight or command smoke checks in the project context."},
            {"command": _cmd(command, f"build-fix -p {project} --json"), "format": "json", "purpose": "Run the failed-task repair loop with validation and verdict output."},
            {"command": _cmd(command, f'skill run ralplan -p {project} --provider codex --input "requirement" --json'), "format": "json", "purpose": "Run an enabled local workflow skill and return structured output."},
            {"command": _cmd(command, f"wiki query <keyword> -p {project} --json"), "format": "json", "purpose": "Search the local project wiki for durable project knowledge."},
            {"command": _cmd(command, f"note show -p {project} --json"), "format": "json", "purpose": "Read persistent project working memory."},
            {"command": _cmd(command, f"trace -p {project} --json"), "format": "json", "purpose": "Return recent project activity across tasks, logs, services, and workflow state."},
        ],
        "commands": [
            {"name": "init", "syntax": _cmd(command, "init <path>"), "purpose": "Initialize a project and generate AGENTS.toml.", "when_to_use": "Use when connecting a repository for the first time.", "examples": [_cmd(command, "init ."), _cmd(command, "init D:\\repo -n demo")]},
            {"name": "setup", "syntax": _cmd(command, "setup [path] [--name <project-name>] [--dry-run] [--json]"), "purpose": "Prepare project .codepilot state, sync AGENTS.toml, and register the project.", "when_to_use": "Use before installing hooks or when preparing CodePilot project state.", "examples": [_cmd(command, "setup ."), _cmd(command, "setup . --dry-run --json")]},
            {"name": "self_update", "syntax": _cmd(command, f'self-update -p {project} --dry-run [--provider codex] "improvement goal" [--json]'), "purpose": "Run a project-local self-improvement dry run that collects evidence and produces an upgrade plan without changing code.", "when_to_use": "Use when CodePilot should audit its own project state before creating tasks or making changes.", "examples": [_cmd(command, f'self-update -p {project} --dry-run --json "Improve multi-agent compatibility"')]},
            {"name": "goal", "syntax": f'{command} "requirement text"', "purpose": "Submit a natural-language requirement for CodePilot to classify, plan, and optionally execute.", "when_to_use": "Use when another AI wants CodePilot to own the implementation workflow.", "examples": [f'{command} "Fix task retry logic and add tests"']},
            {"name": "go", "syntax": _cmd(command, f"go <requirement-or-question> [--project {project}]"), "purpose": "Explicit natural-language entrypoint for questions, planning, and execution.", "when_to_use": "Use when project/planner/executor options must be explicit.", "examples": [_cmd(command, f'go "How many tasks are complete?" -p {project}'), _cmd(command, f'go "Fix task retry logic and add tests" -p {project}')]},
            {"name": "chat", "syntax": _cmd(command, f"chat [-p {project}] [-a opencode]"), "purpose": "Start the CodePilot-managed OpenCode TUI with isolated config, MCP, commands, provider/model, and permissions.", "when_to_use": "Use for persistent interactive sessions.", "examples": [_cmd(command, f"chat -p {project} -a opencode")]},
            {"name": "status", "syntax": _cmd(command, f"status -p {project} [-v|--json]"), "purpose": "Show task overview, running state, and latest output.", "when_to_use": "Use to check backlog, stuck tasks, or active phases.", "examples": [_cmd(command, f"status -p {project} -v"), _cmd(command, f"status -p {project} --json")]},
            {"name": "hud", "syntax": _cmd(command, f"hud [-p {project}] [--all] [--preset minimal|focused|full] [--watch|--json]"), "purpose": "Show a lightweight workflow HUD with queue, running tasks, activity, and services.", "when_to_use": "Use for quick workspace health checks.", "examples": [_cmd(command, f"hud -p {project} --preset full --json")]},
            {"name": "task_logs", "syntax": _cmd(command, "task logs <task_id> [--tail N|--full]"), "purpose": "Read real-time or historical task logs.", "when_to_use": "Use for task execution details and error context.", "examples": [_cmd(command, "task logs 7"), _cmd(command, "task logs 7 --tail 50")]},
            {"name": "task_show", "syntax": _cmd(command, "task show <task_id> [--logs|--json]"), "purpose": "Read full task metadata, content, errors, delivery record, and log summary.", "when_to_use": "Use when the exact task id is known.", "examples": [_cmd(command, "task show 7 --json")]},
            {"name": "task_retry", "syntax": _cmd(command, "task retry <task_id>"), "purpose": "Reset a failed/cancelled task and put it back into backlog.", "when_to_use": "Use for manual requeueing.", "examples": [_cmd(command, "task retry 7")]},
            {"name": "task_stop", "syntax": _cmd(command, "task stop <task_id> [-m <reason>]"), "purpose": "Stop a running task.", "when_to_use": "Use when a task is stuck or going in the wrong direction.", "examples": [_cmd(command, 'task stop 7 -m "wrong direction"')]},
            {"name": "run", "syntax": _cmd(command, f"run -p {project} [--executor builtin|dispatch]"), "purpose": "Execute backlog tasks.", "when_to_use": "Use when tasks already exist and should run now.", "examples": [_cmd(command, f"run -p {project} --executor builtin --no-auto-commit")]},
            {"name": "build_fix", "syntax": _cmd(command, f"build-fix -p {project} [--task-id <id>] [--verify-command <cmd>] [--json]"), "purpose": "Collect failure context, retry repair, run validation, and produce a verdict.", "when_to_use": "Use when failed tasks need a complete repair loop.", "examples": [_cmd(command, f'build-fix -p {project} --task-id 7 --verify-command "pytest tests/test_x.py -q" --json')]},
            {"name": "doctor", "syntax": _cmd(command, f"doctor [--project {project}] [--services] [--fix] [--json]"), "purpose": "Check environment, configuration, CLI tools, API keys, database, and services.", "when_to_use": "Use for environment or setup troubleshooting.", "examples": [_cmd(command, "doctor --json"), _cmd(command, "doctor --fix --json")]},
            {"name": "inspect", "syntax": _cmd(command, f"inspect -p {project} [--once|--status|--stop|--json]"), "purpose": "Inspect project signals and create candidate tasks.", "when_to_use": "Use to discover technical debt, failed-task patterns, and improvement candidates.", "examples": [_cmd(command, f"inspect -p {project} --once")]},
            {"name": "explore", "syntax": _cmd(command, f"explore --prompt <question> [-p {project}] [--use-wiki|--no-wiki] [--json]"), "purpose": "Read-only exploration of files, Git, task logs, wiki, and inspect signals.", "when_to_use": "Use when planning needs local evidence but should not modify anything.", "examples": [_cmd(command, 'explore --prompt "find task template" --json')]},
            {"name": "clarify", "syntax": _cmd(command, f"clarify [-p {project}] [--quick|--standard] <requirement> [--json]"), "purpose": "Generate a pre-execution requirement specification artifact.", "when_to_use": "Use when a requirement is still vague but should not create tasks yet.", "examples": [_cmd(command, f'clarify -p {project} "Improve doctor" --json')]},
            {"name": "plan", "syntax": _cmd(command, f"plan [-p {project}] <requirement> [--from-spec <path>] [--use-wiki|--no-wiki] [--json]"), "purpose": "Generate a reviewable plan artifact with scope, risks, verification matrix, and task candidates.", "when_to_use": "Use when requirements are ready for plan review but should not enter backlog.", "examples": [_cmd(command, f'plan -p {project} "Add explore" --json')]},
            {"name": "workflow_status", "syntax": _cmd(command, f"workflow status -p {project} [--mode <mode>] --json"), "purpose": "Read workflow mode state and agent session context, including artifact paths and next-action source.", "when_to_use": "Use before advancing a clarify/plan artifact or when inspecting current workflow state.", "examples": [_cmd(command, f"workflow status -p {project} --json"), _cmd(command, f"workflow status -p {project} --mode plan --json")]},
            {"name": "workflow_next", "syntax": _cmd(command, f"workflow next -p {project} [--mode <mode>] [--list | --action <id>] [--allow-high-risk] --json"), "purpose": "List or safely execute artifact next_actions through a fixed allowlist; it never executes suggested_command strings.", "when_to_use": "Use after clarify/plan returns next_actions and an external agent needs to advance by action id.", "examples": [_cmd(command, f"workflow next -p {project} --list --json"), _cmd(command, f"workflow next -p {project} --action plan_from_spec --json"), _cmd(command, f"workflow next -p {project} --action import_tasks --json")]},
            {"name": "wiki", "syntax": _cmd(command, "wiki <add|list|query|update|delete|refresh|lint|ingest> ..."), "purpose": "Maintain local Markdown project knowledge.", "when_to_use": "Use to record or query build commands, architecture facts, failure modes, and decisions.", "examples": [_cmd(command, f'wiki query -p {project} "build" --json')]},
            {"name": "note", "syntax": _cmd(command, "note <add|show|prune|clear> ..."), "purpose": "Maintain persistent project working memory.", "when_to_use": "Use to preserve important context across sessions.", "examples": [_cmd(command, f'note add -p {project} "pytest -q is the main validation command"')]},
            {"name": "trace", "syntax": _cmd(command, f"trace [-p {project}] [--task <task_id>] [--limit N] [--json]"), "purpose": "Show recent activity across task lifecycle, logs, service heartbeats, and workflow state.", "when_to_use": "Use to understand what happened recently.", "examples": [_cmd(command, f"trace -p {project} --limit 30")]},
            {"name": "ui", "syntax": _cmd(command, "ui [--host 127.0.0.1] [--port 8766] | ui <start|status|logs|stop|restart>"), "purpose": "Run or manage the Web UI.", "when_to_use": "Use for graphical multi-project overview or persistent UI service management.", "examples": [_cmd(command, "ui --no-open --port 8877"), _cmd(command, "ui start")]},
            {"name": "daemon", "syntax": _cmd(command, f"daemon -p {project} [--status|--stop]"), "purpose": "Continuously poll backlog and execute tasks for a project.", "when_to_use": "Use when tasks should run continuously in the background.", "examples": [_cmd(command, f"daemon -p {project} --status")]},
            {"name": "event", "syntax": _cmd(command, f"event <schema|list|register|enable|disable|test> [--project {project}] [--json]"), "purpose": "Manage project event sink registry and event schema.", "when_to_use": "Use for hook/event plugin integration.", "examples": [_cmd(command, "event schema --json"), _cmd(command, f"event list -p {project} --json")]},
            {"name": "hook", "syntax": _cmd(command, f"hook <plan|validate|test|logs|install|uninstall> -p {project} [--json]"), "purpose": "Manage project hook registry and lifecycle event validation.", "when_to_use": "Use to verify hook/event integrations.", "examples": [_cmd(command, f"hook validate -p {project} --json")]},
            {"name": "exec", "syntax": _cmd(command, f"exec -p {project} --provider codex|claude|gemini|custom [--dry-run] [--json] -- <command...>"), "purpose": "Run provider-neutral command smoke checks with project-local audit events.", "when_to_use": "Use to verify agent CLIs or custom commands in project context.", "examples": [_cmd(command, f"exec -p {project} --provider codex --dry-run --json -- codex --version")]},
            {"name": "skill", "syntax": _cmd(command, f"skill <list|search|show|enable|disable|run> -p {project} [--json]"), "purpose": "Manage and run local workflow skill catalog entries.", "when_to_use": "Use to discover or run project workflow skills.", "examples": [_cmd(command, f"skill list -p {project} --json")]},
            {"name": "webhook", "syntax": _cmd(command, "webhook [--host 127.0.0.1] [--port 8765]"), "purpose": "Start the lightweight HTTP webhook service.", "when_to_use": "Use when external systems need to POST tasks or receive status notifications.", "examples": [_cmd(command, "webhook --host 127.0.0.1 --port 8765")]},
            {"name": "feishu", "syntax": _cmd(command, "feishu <start|status|logs|stop|run|handle-event>"), "purpose": "Manage the Feishu long-connection bot.", "when_to_use": "Use for Feishu status cards, Q&A, task control, and notifications.", "examples": [_cmd(command, "feishu start"), _cmd(command, "feishu status")]},
            {"name": "binary_prepare", "syntax": _cmd(command, "binary prepare --version <version>"), "purpose": "Update version, build the current platform, generate a release directory, and verify it.", "when_to_use": "Use to prepare a local release package.", "examples": [_cmd(command, "binary prepare --version 0.7.5")]},
            {"name": "binary_release", "syntax": _cmd(command, "binary release [--build-current] [--artifact platform=path]"), "purpose": "Assemble existing binaries into a standard release directory.", "when_to_use": "Use when preparing artifacts for release.", "examples": [_cmd(command, "binary release --build-current"), _cmd(command, f"binary release --artifact linux-x86_64=dist/binary/linux-x86_64/{binary}")]},
            {"name": "binary_verify", "syntax": _cmd(command, "binary verify [--release-dir <release-dir>]"), "purpose": "Verify release directory, archives, and checksums.", "when_to_use": "Use before publishing.", "examples": [_cmd(command, "binary verify")]},
            {"name": "ai_template", "syntax": _cmd(command, "ai template [--format md|json|guide] [--language en|zh-CN]"), "purpose": "Return the task template, machine-readable schema, or filling guide.", "when_to_use": "Use before external AI batch-imports tasks with `add -f`.", "examples": [_cmd(command, "ai template --format json"), _cmd(command, "ai template --format guide --language zh-CN")]},
        ],
        "workflows": [
            {"name": "Submit and execute a new requirement", "steps": [_cmd(command, "setup ."), f'{command} "Implement a requirement"', _cmd(command, f"status -p {project} -v"), _cmd(command, "task show <task_id>"), _cmd(command, "task logs <task_id>")]},
            {"name": "Use the OpenCode workflow in chat or Feishu", "steps": ["Ask questions directly, such as `How many tasks are done?`.", "Submit requirements as natural language; OpenCode can call CodePilot MCP planning and task tools.", "Use explicit operations like `retry 123` or `tasks failed`, or describe the operation in natural language.", "Call `clarify` / `plan` explicitly when deterministic artifacts are needed."]},
            {"name": "Review an artifact and safely advance its next action", "steps": [_cmd(command, f"workflow status -p {project} --json"), _cmd(command, f"workflow next -p {project} --list --json"), "# Choose an action id from the list; do not execute suggested_command strings.", _cmd(command, f"workflow next -p {project} --action <id> --json")]},
            {"name": "Debug a running task", "steps": [_cmd(command, f"status -p {project} -v"), _cmd(command, "task show <task_id>"), _cmd(command, "task logs <task_id> --tail 80"), _cmd(command, "task stop <task_id>")]},
            {"name": "Retry a failed or cancelled task", "steps": [_cmd(command, "task logs <task_id> --tail 80"), _cmd(command, "task retry <task_id>"), _cmd(command, f"run -p {project}")]},
            {"name": "Prepare a release package", "steps": [_cmd(command, "binary prepare --version <version>"), _cmd(command, "binary verify")]},
            {"name": "Import externally planned tasks", "steps": [_cmd(command, "ai template --format json"), "# Render tasks.json according to the schema; each item needs complete English task-template markdown in `content`.", _cmd(command, f"add -p {project} -f tasks.json"), _cmd(command, f"status -p {project} -v")]},
        ],
    }


def manifest_json(
    indent: int = 2,
    *,
    version: str | None = None,
    command_name: str = "codepilot",
    binary_name: str = "codepilot",
    language: str = "en",
) -> str:
    """Serialize the AI command manifest as JSON."""
    return json.dumps(
        command_manifest(version=version, command_name=command_name, binary_name=binary_name, language=language),
        ensure_ascii=False,
        indent=indent,
    )
