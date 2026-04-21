"""Machine-readable command manifest and AI-facing usage guides."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from codepilot import __version__


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
                "需要结构化结果时，优先使用 `status --json`、`show --json`、`doctor --json`、`find --json`、`ai manifest`。",
                f"如果目标是提交一个自然语言需求，直接调用 `{command} \"需求文本\"` 或 `{command} go \"需求文本\"`。",
                f"如果目标是发布产物，优先调用 `{_cmd(command, 'release prepare --version <版本号>')}`。",
                f"如果任务处于运行中，先用 `{_cmd(command, 'status -p <项目名> -v')}` 查看阶段，再决定是否 `logs` 或 `stop`。",
                f"如果任务失败或被取消，且需要重新排队，使用 `{_cmd(command, 'retry <task_id>')}`。",
            ],
        "structured_outputs": [
            {
                "command": _cmd(command, "ai manifest"),
                "format": "json",
                "purpose": "输出完整命令清单、常见工作流和调用建议。",
            },
            {
                "command": _cmd(command, "status -p <项目名> --json"),
                "format": "json",
                "purpose": "获取项目任务状态和任务列表。",
            },
            {
                "command": _cmd(command, "find <关键词> -p <项目名> --json"),
                "format": "json",
                "purpose": "搜索任务并获取结构化结果。",
            },
            {
                "command": _cmd(command, "show <task_id> --json"),
                "format": "json",
                "purpose": "精确获取单个任务的完整详情和历史日志记录。",
            },
            {
                "command": _cmd(command, "doctor --json"),
                "format": "json",
                "purpose": "检查本机运行环境、CLI 工具、配置和数据库可用性。",
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
                "name": "goal",
                "syntax": f'{command} "需求文本"',
                "purpose": "提交一个自然语言需求，让 CodePilot 自动判断是否拆分并按配置执行。",
                "when_to_use": "其他 AI 想把高层目标交给 CodePilot 自己执行时。",
                "examples": [f'{command} "修复任务重试逻辑并补测试"'],
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
                "name": "logs",
                "syntax": _cmd(command, "logs <task_id> [--tail N|--full]"),
                "purpose": "查看任务实时日志或历史日志。",
                "when_to_use": "需要了解任务执行细节或错误上下文。",
                "examples": [_cmd(command, "logs 7"), _cmd(command, "logs 7 --tail 50")],
            },
            {
                "name": "show",
                "syntax": _cmd(command, "show <task_id> [--logs|--json]"),
                "purpose": "精确查看单个任务的完整元数据、任务内容、错误信息、交付记录和日志记录摘要。",
                "when_to_use": "已经知道任务 ID，需要完整任务详情而不是状态列表摘要时。",
                "examples": [_cmd(command, "show 7"), _cmd(command, "show 7 --json")],
            },
            {
                "name": "doctor",
                "syntax": _cmd(command, "doctor [--json]"),
                "purpose": "检查 CodePilot 当前运行环境、配置、CLI 工具、API Key 和任务数据库。",
                "when_to_use": "接入新机器、排查环境问题，或需要机器可读环境健康状态时。",
                "examples": [_cmd(command, "doctor"), _cmd(command, "doctor --json")],
            },
            {
                "name": "stop",
                "syntax": _cmd(command, "stop <task_id> [-m 原因]"),
                "purpose": "停止运行中的任务。",
                "when_to_use": "任务卡住、执行方向错误、需要强制终止时。",
                "examples": [_cmd(command, "stop 7"), _cmd(command, 'stop 7 -m "方向错误，停止重跑"')],
            },
            {
                "name": "retry",
                "syntax": _cmd(command, "retry <task_id>"),
                "purpose": "手动重试指定任务，重置运行态并重新放回 backlog。",
                "when_to_use": "任务 failed/cancelled 后需要人工重新触发时。",
                "examples": [_cmd(command, "retry 7")],
            },
            {
                "name": "run",
                "syntax": _cmd(command, "run -p <项目名> [--executor builtin|dispatch]"),
                "purpose": "执行 backlog 中的任务。",
                "when_to_use": "已存在 backlog，想手动触发执行。",
                "examples": [_cmd(command, "run -p codepilot-dev --executor builtin --no-auto-commit")],
            },
            {
                "name": "ui",
                "syntax": _cmd(command, "ui [--host 127.0.0.1] [--port 8766]"),
                "purpose": "启动本地 Web UI，图形化查看项目、任务和状态，并执行重试 / 停止 / 插队。",
                "when_to_use": "需要图形化总览多个项目，或需要人工点击干预任务时。",
                "examples": [_cmd(command, "ui"), _cmd(command, "ui --no-open --port 8877")],
            },
            {
                "name": "release_prepare",
                "syntax": _cmd(command, "release prepare --version <版本号>"),
                "purpose": "更新版本号、构建当前平台、生成发布目录并自动校验。",
                "when_to_use": "准备一个可交付的本地发布包时。",
                "examples": [_cmd(command, "release prepare --version 0.1.1")],
            },
            {
                "name": "release_bundle",
                "syntax": _cmd(command, "release bundle [--build-current] [--artifact 平台=路径]"),
                "purpose": "把已有二进制整理成标准发布目录。",
                "when_to_use": "已有一个或多个平台二进制，准备打包发布时。",
                "examples": [
                    _cmd(command, "release bundle --build-current"),
                    _cmd(command, f"release bundle --artifact linux-x86_64=dist/binary/linux-x86_64/{binary}"),
                ],
            },
            {
                "name": "release_verify",
                "syntax": _cmd(command, "release verify [--release-dir 发布目录]"),
                "purpose": "校验发布目录、压缩包内容和校验值。",
                "when_to_use": "发布前自检或 CI 验证时。",
                "examples": [
                    _cmd(command, "release verify"),
                    _cmd(command, "release verify --release-dir dist/release/codepilot-0.1.0-summary"),
                ],
            },
        ],
        "workflows": [
            {
                "name": "提交一个新需求并执行",
                "steps": [
                    _cmd(command, "init ."),
                    f'{command} "实现一个需求"',
                    _cmd(command, "status -p <项目名> -v"),
                    _cmd(command, "show <task_id>"),
                    _cmd(command, "logs <task_id>"),
                ],
            },
            {
                "name": "排障运行中的任务",
                "steps": [
                    _cmd(command, "status -p <项目名> -v"),
                    _cmd(command, "show <task_id>"),
                    _cmd(command, "logs <task_id> --tail 80"),
                    _cmd(command, "stop <task_id>"),
                ],
            },
            {
                "name": "重试失败或取消的任务",
                "steps": [
                    _cmd(command, "logs <task_id> --tail 80"),
                    _cmd(command, "retry <task_id>"),
                    _cmd(command, "run -p <项目名>"),
                ],
            },
            {
                "name": "准备一个发布包",
                "steps": [
                    _cmd(command, "release prepare --version <版本号>"),
                    _cmd(command, "release verify"),
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

这份手册是写给其他 AI/Agent 的，不是写给人类终端用户的。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时，优先使用 JSON 输出命令。
3. 需要提交高层需求时，直接调用自然语言入口，不要先自己拆任务，除非你明确要控制拆分策略。
4. 看到任务处于 `in_progress` 时，先查 `status -v` 和 `logs`，不要盲目重复触发 `run`。
5. 任务失败或取消后，如需人工重新排队，使用 `{_cmd(command, "retry <task_id>")}`。
6. 准备发布包时，优先使用 `{_cmd(command, "release prepare --version <版本号>")}`。

## 推荐命令

### 1. 初始化项目

```bash
{_cmd(command, "init .")}
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

### 4. 精确查看单个任务

```bash
{_cmd(command, "show <task_id>")}
{_cmd(command, "show <task_id> --json")}
```

### 5. 环境自检

```bash
{_cmd(command, "doctor")}
{_cmd(command, "doctor --json")}
```

### 6. 查看日志

```bash
{_cmd(command, "logs <task_id>")}
{_cmd(command, "logs <task_id> --tail 80")}
```

### 7. 停止任务

```bash
{_cmd(command, "stop <task_id>")}
```

### 8. 手动重试任务

```bash
{_cmd(command, "retry <task_id>")}
```

### 9. 发布

最推荐：

```bash
{_cmd(command, "release prepare --version 0.1.1")}
```

只打包：

```bash
{_cmd(command, "release bundle --build-current")}
```

校验发布目录：

```bash
{_cmd(command, "release verify")}
```

### 10. 图形界面

```bash
{_cmd(command, "ui")}
```

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
"""


def ai_prompt_text(*, command_name: str = "codepilot") -> str:
    """Return a compact prompt for another AI to operate CodePilot safely."""
    command = normalize_command_name(command_name)
    return (
        "你正在调用 CodePilot 这个本地 CLI。优先使用非交互命令。"
        f"提交需求时直接用 `{command} \"需求文本\"`。"
        f"查看状态时优先用 `{_cmd(command, 'status -p <项目名> --json')}`，"
        f"精确查看单个任务用 `{_cmd(command, 'show <task_id> --json')}`，"
        f"检查本机环境用 `{_cmd(command, 'doctor --json')}`，"
        f"排障时用 `{_cmd(command, 'logs <task_id>')}`，停止任务用 `{_cmd(command, 'stop <task_id>')}`，"
        f"重试失败任务用 `{_cmd(command, 'retry <task_id>')}`。"
        f"如果需要人工介入或图形化查看，启动 `{_cmd(command, 'ui')}`。"
        f"准备发布包时优先用 `{_cmd(command, 'release prepare --version <版本号>')}`。"
        f"如果需要完整命令清单，调用 `{_cmd(command, 'ai manifest')}`。"
    )
