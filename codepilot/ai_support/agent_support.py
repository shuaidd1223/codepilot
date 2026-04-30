"""Machine-readable command manifest and AI-facing usage guides."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from codepilot import __version__
from codepilot.core.task_template import required_task_template_headings


def normalize_command_name(command_name: str | None = None) -> str:
    """Normalize a shell command label for published AI docs."""
    raw = (command_name or "").strip()
    if not raw:
        return "codepilot"

    if " " in raw:
        return raw

    candidate = Path(raw).name
    stem = Path(candidate).stem
    return stem or candidate or "codepilot"


def runtime_command_name() -> str:
    """Infer the command name currently used to invoke CodePilot."""
    argv0 = (sys.argv[0] or "").strip()
    if not argv0:
        return "codepilot"

    basename = Path(argv0).name.lower()
    if basename.startswith("pytest") or basename in {"-c", "-m"}:
        return "codepilot"
    if basename in {"python", "python.exe", "py", "py.exe", "__main__.py"}:
        return "python -m codepilot"

    if basename.endswith(".py"):
        return "python -m codepilot"

    return normalize_command_name(argv0)


def _cmd(command_name: str, suffix: str) -> str:
    command = normalize_command_name(command_name)
    return f"{command} {suffix}".strip()


def command_manifest(
    *,
    version: str | None = None,
    command_name: str = "codepilot",
    binary_name: str = "codepilot",
) -> dict[str, Any]:
    """Return a machine-readable manifest for other AI tools."""
    command = normalize_command_name(command_name)
    manifest_version = (version or __version__).strip()
    binary = normalize_command_name(binary_name)
    return {
        "name": "CodePilot",
        "version": manifest_version,
        "command_name": command,
        "description": "本地工程工作流 CLI，可把自然语言需求转换成任务，并自动规划、执行、审查和发布。",
            "calling_principles": [
                "优先使用非交互命令，避免 chat 模式，除非明确需要持续会话。",
                "需要结构化结果时，优先使用 `hud --json`、`status --json`、`task show --json`、`doctor --json`、`task find --json`、`ai manifest`。",
                f"如果目标是提交一个自然语言需求，直接调用 `{command} \"需求文本\"` 或 `{command} go \"需求文本\"`。",
                "在 chat、Web UI 会话和飞书自由文本里，疑似需求/任务不会自动执行；要创建工作请显式使用 `需求 <内容>` / `# <内容>` 或 `任务 <内容>` / `! <内容>`。",
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
                "syntax": _cmd(command, "chat [-p <项目名>] [--no-ui]"),
                "purpose": "启动持续自然语言会话，支持问答、命令和显式需求/任务提交。",
                "when_to_use": "需要连续追问项目状态或处理澄清流程时。自由文本疑似需求会先要求确认；创建需求用 `需求 <内容>` 或 `# <内容>`，创建单步任务用 `任务 <内容>` 或 `! <内容>`。",
                "examples": [
                    _cmd(command, "chat -p codepilot-dev"),
                    "在 chat 中输入：需求 优化飞书任务面板",
                    "在 chat 中输入：? 当前有多少任务，完成了多少",
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
                "examples": [_cmd(command, "binary prepare --version 0.1.1")],
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
                    _cmd(command, "binary verify --release-dir dist/release/codepilot-0.1.0-summary"),
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
                "name": "在会话或飞书中安全区分问答与创建工作",
                "steps": [
                    "普通提问：直接输入 `当前有多少任务，完成了多少` 或使用 `? 当前项目状态怎么样`",
                    "创建需求：输入 `需求 <内容>` 或 `# <内容>`",
                    "创建单步任务：输入 `任务 <内容>` 或 `! <内容>`",
                    "疑似需求但缺少显式前缀时，CodePilot 只返回确认提示，不会直接执行。",
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


def manifest_json(
    indent: int = 2,
    *,
    version: str | None = None,
    command_name: str = "codepilot",
    binary_name: str = "codepilot",
) -> str:
    """Serialize the AI command manifest as JSON."""
    return json.dumps(
        command_manifest(version=version, command_name=command_name, binary_name=binary_name),
        ensure_ascii=False,
        indent=indent,
    )


def ai_guide_markdown(*, command_name: str = "codepilot") -> str:
    """Return an AI-oriented Markdown guide."""
    command = normalize_command_name(command_name)
    return f"""# CodePilot AI 调用手册

这份手册是写给其他 AI / Agent 的静态入口。最新机器可读清单以 `{_cmd(command, "ai manifest")}` 为准，最新 Markdown 手册以 `{_cmd(command, "ai guide")}` 为准。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时优先使用 `--json` 或 `{_cmd(command, "ai manifest")}`。
3. 提交高层需求时直接调用 `{command} "需求文本"` 或 `{_cmd(command, 'go "需求文本"')}`。
4. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态这类问题应作为问答处理。
5. 在 `chat`、Web UI 会话和飞书自由文本中，创建工作必须显式输入 `# <需求>` / `需求 <内容>` 或 `! <任务>` / `任务 <内容>`。
6. 任务运维统一使用 `{_cmd(command, "task ...")}`。
7. 发布统一使用 `{_cmd(command, "binary ...")}`。
8. 外部 AI 直接投递任务前必须读取 `{_cmd(command, "ai template --format json")}`。

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
```

`clarify` 和 `plan` 不创建 backlog、不启动执行器。

### 4. 状态与证据

```bash
{_cmd(command, "status -p <项目名> --json")}
{_cmd(command, "hud -p <项目名> --preset full --json")}
{_cmd(command, 'explore -p <项目名> --prompt "find task template" --json')}
{_cmd(command, "trace -p <项目名> --limit 30 --json")}
{_cmd(command, 'wiki query -p <项目名> "构建" --json')}
{_cmd(command, "note show -p <项目名> --json")}
```

`explore` 是只读入口，不写文件、不改 Git、不启动服务、不安装依赖、不执行测试。

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
{_cmd(command, "build-fix -p <项目名> --task-id <task_id> --dry-run")}
{_cmd(command, "build-fix -p <项目名> --task-id <task_id> --json")}
```

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

飞书自由文本建议：

```text
? 当前项目状态怎么样
# 优化飞书任务面板
! 修复一个明确的小问题
```

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
{_cmd(command, "binary prepare --version 0.1.1")}
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
7. 在 `chat`、Web UI 会话和飞书自由文本中，疑似需求/任务不会直接执行；创建工作必须显式输入 `需求 <内容>` / `# <内容>` 或 `任务 <内容>` / `! <内容>`。
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
{_cmd(command, "binary prepare --version 0.1.1")}
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

### 11. 交互会话的显式前缀

`chat`、Web UI 会话和飞书自由文本会优先保护执行边界：疑似需求/任务没有显式前缀时，只返回确认提示，不会创建任务。

```text
? 当前项目状态怎么样
问题 当前有多少任务，完成了多少
需求 优化飞书任务面板
# 修复任务通知卡片样式
任务 重跑失败任务 12
! 修复一个明确的小问题
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


def ai_prompt_text(*, command_name: str = "codepilot") -> str:
    """Return a compact prompt for another AI to operate CodePilot safely."""
    command = normalize_command_name(command_name)
    return (
        "你正在调用 CodePilot 这个本地 CLI。优先使用非交互命令。"
        f"提交需求时直接用 `{command} \"需求文本\"`。"
        "在 chat、Web UI 会话和飞书自由文本里，疑似需求/任务不会直接执行；"
        "要创建需求用 `需求 <内容>` 或 `# <内容>`，要创建单步任务用 `任务 <内容>` 或 `! <内容>`；"
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

