"""codepilot run 命令：从 SQLite 读取 backlog 任务，调用 dispatch 执行.

跨平台支持：Windows (PowerShell 7/5, cmd) / Linux (bash, zsh) / macOS (bash, zsh)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import click

from codepilot import db
from codepilot.webhook import notify_task_status
from codepilot.commands.status import _resolve_project


# ═══════════════════════════════════════════════════════════════════════════════
# 跨平台 Shell 检测与选择
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ShellInfo:
    """Shell 信息."""
    executable: str  # 可执行命令
    args: list[str] = None  # 额外参数（如 ["-Command"] 或 ["-File"]）
    is_powershell: bool = False
    is_bash: bool = False
    version_hint: str = ""

    def __post_init__(self):
        if self.args is None:
            self.args = []


def detect_best_shell(preferred: Optional[str] = None) -> ShellInfo:
    """
    检测当前平台可用的最佳 Shell.

    Windows 优先级: pwsh > powershell.exe > cmd.exe
    Unix 优先级: zsh > bash > sh
    """
    system = platform.system().lower()
    is_windows = system == "windows"

    # 用户偏好
    if preferred:
        preferred = preferred.lower()
        if preferred in ("pwsh", "powershell7"):
            if shutil.which("pwsh"):
                return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
        elif preferred == "powershell":
            if shutil.which("powershell.exe"):
                return ShellInfo("powershell.exe", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 5")
            elif shutil.which("pwsh"):
                return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
        elif preferred in ("bash", "zsh", "sh"):
            shell = shutil.which(preferred)
            if shell:
                return ShellInfo(shell, [], is_bash=True, version_hint=preferred)

    # 自动检测
    if is_windows:
        # Windows: 优先 PowerShell 7
        if shutil.which("pwsh"):
            return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
        # 然后 PowerShell 5
        if shutil.which("powershell.exe"):
            return ShellInfo("powershell.exe", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 5")
        # 最后 cmd
        if shutil.which("cmd.exe"):
            return ShellInfo("cmd.exe", ["/C"], version_hint="cmd")
    else:
        # Unix-like: 优先 zsh
        for shell in ["zsh", "bash", "sh"]:
            path = shutil.which(shell)
            if path:
                return ShellInfo(path, [], is_bash=True, version_hint=shell)

    # 兜底
    return ShellInfo("sh", ["-c"], version_hint="sh")


def build_script_command(
    shell: ShellInfo,
    script_path: Path,
    script_args: list[str],
) -> tuple[list[str], str]:
    """
    构建跨平台的脚本执行命令.

    Returns:
        (command_list, description)
    """
    if shell.is_powershell:
        # PowerShell: -File 参数
        cmd = [shell.executable] + shell.args + [
            "-File", str(script_path)
        ] + script_args
        desc = f"{shell.version_hint} -File {script_path.name}"
    elif shell.is_bash:
        # Bash/Zsh: 直接执行
        cmd = [shell.executable] + shell.args + [
            f"chmod +x {script_path} 2>/dev/null; {script_path} {' '.join(script_args)}"
        ]
        desc = f"{shell.version_hint} {script_path.name}"
    else:
        # cmd.exe
        cmd = [shell.executable] + shell.args + [
            f"{script_path} {' '.join(script_args)}"
        ]
        desc = f"cmd {script_path.name}"

    return cmd, desc


# ═══════════════════════════════════════════════════════════════════════════════
# Dispatch 脚本查找
# ═══════════════════════════════════════════════════════════════════════════════

def _find_dispatch_script(project_path: str | None = None) -> tuple[Path, str]:
    """
    按优先级查找 dispatch 脚本.

    Returns:
        (script_path, source_description)
    """
    candidates: list[tuple[Path, str]] = []

    # 1. 环境变量（最高优先级）
    env_path = os.environ.get("CODEPILOT_DISPATCH_PATH")
    if env_path:
        candidates.append((Path(env_path), "CODEPILOT_DISPATCH_PATH"))

    # 2. AGENTS.toml
    if project_path:
        agents_toml = Path(project_path) / "AGENTS.toml"
        if agents_toml.exists():
            try:
                content = agents_toml.read_text(encoding="utf-8")
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("dispatch_path"):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            candidates.append((Path(val), "AGENTS.toml"))
                            break
            except Exception:
                pass

    # 3. codepilot 包内 scripts/
    pkg_scripts = Path(__file__).parent.parent.parent / "scripts" / "task-dispatch.ps1"
    candidates.append((pkg_scripts, "codepilot 包"))

    # 4. ~/.codepilot/scripts/
    user_scripts = Path.home() / ".codepilot" / "scripts" / "task-dispatch.ps1"
    candidates.append((user_scripts, "~/.codepilot/scripts"))

    # 5. aigent-dev（兼容旧配置）
    aigent_dispatch = Path.home() / "myCode" / "aigent-dev" / "scripts" / "automation" / "task-dispatch.ps1"
    candidates.append((aigent_dispatch, "aigent-dev"))

    # 查找脚本扩展名
    system = platform.system().lower()
    extensions = [".ps1"]  # PowerShell
    if system != "windows":
        extensions.append(".sh")  # Bash

    for path, source in candidates:
        # 尝试不同扩展名
        if path.suffix == "":
            for ext in extensions:
                if path.with_suffix(ext).exists():
                    return path.with_suffix(ext), source
        if path.exists():
            return path, source

    raise FileNotFoundError(
        "task-dispatch script not found. "
        "请设置 CODEPILOT_DISPATCH_PATH 环境变量，或在 AGENTS.toml 中配置 dispatch_path。"
    )


def _is_powershell_script(script_path: Path) -> bool:
    """判断是否为 PowerShell 脚本."""
    return script_path.suffix.lower() in (".ps1", ".psm1")


# ═══════════════════════════════════════════════════════════════════════════════
# 任务文件构建
# ═══════════════════════════════════════════════════════════════════════════════

def _build_task_md(task: dict, project: dict) -> str:
    """从 task dict 构建符合 dispatch 预期的 task.md 内容."""
    depends = task.get("depends_on") or ""
    if isinstance(depends, str):
        try:
            deps = json.loads(depends)
        except Exception:
            deps = []
    else:
        deps = depends or []

    depends_str = ", ".join(str(d) for d in deps) if deps else "无"
    content = task.get("content") or ""

    lines = [
        f"# Task: {task['title']}",
        "",
        f"- **Agent:** {task['agent']}",
        f"- **Builder:** {task.get('builder') or 'codex'}",
        f"- **Reviewer:** {task.get('reviewer') or 'claude'}",
        f"- **Priority:** {task['priority']}",
        f"- **Depends on:** {depends_str}",
        "",
    ]

    if content:
        lines.append(content)
    else:
        lines.append("## 任务描述\n\n（待填充）")

    return "\n".join(lines)


def _pick_task_file(project_path: Path, task_id: int) -> Path:
    """在项目的 tasks/backlog/ 下生成任务文件路径."""
    backlog = project_path / "tasks" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    return backlog / f"{task_id:03d}-task.md"


# ═══════════════════════════════════════════════════════════════════════════════
# Dispatch 执行
# ═══════════════════════════════════════════════════════════════════════════════

def _run_dispatch(
    project: dict,
    task_file: Path,
    agent_mode: str,
    shell: Optional[ShellInfo] = None,
    dry_run: bool = False,
) -> tuple[int, str]:
    """
    调用 dispatch 脚本执行任务.

    Returns:
        (exit_code, output)
    """
    if shell is None:
        shell = detect_best_shell()

    dispatch_path, _ = _find_dispatch_script(str(project.get("path", "")))

    # 构建脚本参数
    script_args = [
        "-AgentMode", agent_mode,
        "-TaskFile", str(task_file),
        "-Once",
    ]
    if dry_run:
        script_args.append("-DryRun")

    # 构建命令
    if _is_powershell_script(dispatch_path):
        cmd, desc = build_script_command(shell, dispatch_path, script_args)
    else:
        # Bash script
        cmd = [shell.executable] + shell.args + [
            f"{dispatch_path} {' '.join(script_args)}"
        ]
        desc = f"{shell.version_hint} {dispatch_path.name}"

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,  # 1 小时超时
    )

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    return result.returncode, output


# ═══════════════════════════════════════════════════════════════════════════════
# Worktree 清理
# ═══════════════════════════════════════════════════════════════════════════════

def _cleanup_worktree(project_path: Path, worktree_path: str | None):
    """清理 worktree（任务完成后删除）."""
    if not worktree_path:
        return
    wt = Path(worktree_path)
    if wt.exists() and wt.is_dir():
        try:
            import shutil as sh
            sh.rmtree(wt)
            click.echo(f"[dim]  清理 worktree: {wt}[/dim]")
        except Exception as e:
            click.echo(f"[yellow]  清理 worktree 失败: {e}[/yellow]")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 命令
# ═══════════════════════════════════════════════════════════════════════════════

@click.command()
@click.option(
    "--project", "-p",
    callback=_resolve_project,
    help="项目名称",
)
@click.option("--once", is_flag=True, help="单次运行后退出（默认）")
@click.option("--limit", "-n", type=int, default=1, help="最多执行任务数量")
@click.option("--dry-run", is_flag=True, help="试运行，不真正执行 Agent")
@click.option("--cleanup/--no-cleanup", default=True, help="任务完成后清理 worktree")
@click.option(
    "--shell",
    type=click.Choice(["auto", "pwsh", "powershell", "bash", "zsh"], case_sensitive=False),
    default="auto",
    help="指定使用的 Shell（默认自动检测）",
)
def run(
    project: str | None,
    once: bool,
    limit: int,
    dry_run: bool,
    cleanup: bool,
    shell: str,
):
    """
    从 SQLite backlog 读取任务，调用 dispatch 执行.

    跨平台支持：
    - Windows: PowerShell 7 / PowerShell 5 / cmd
    - Linux/macOS: zsh / bash / sh

    工作流：
    1. 从 tasks.db 取下一个待执行任务（按 P0→P3 优先级）
    2. 将任务内容写入项目的 tasks/backlog/<id>-task.md
    3. 调用 dispatch 脚本执行
    4. 根据 dispatch 结果更新 tasks.db 中的状态
    5. 清理 worktree（可选）
    """
    db.init_db()

    if not project:
        click.echo("[red]错误: 必须指定 --project[/red]")
        return

    proj = db.get_project(project)
    if not proj:
        click.echo(f"[red]错误: 项目 '{project}' 未注册，请先运行 codepilot init[/red]")
        return

    project_path = Path(proj["path"])
    agent_mode = proj.get("default_mode") or "dual"

    # 检测 Shell
    shell_info = detect_best_shell(shell if shell != "auto" else None)
    click.echo(f"[dim]使用 Shell: {shell_info.version_hint}[/dim]")

    executed = 0
    failed = 0

    for i in range(limit):
        # 取下一个 backlog 任务
        tasks = db.next_backlog_task(project)
        if not tasks or not tasks[0]:
            click.echo("[yellow]没有待执行的任务[/yellow]")
            break

        task = tasks[0]
        task_id = task["id"]

        # 写任务文件
        task_file = _pick_task_file(project_path, task_id)
        task_md = _build_task_md(task, proj)
        task_file.write_text(task_md, encoding="utf-8")

        worktree_path = str(project_path.parent / f"{project}-task-{task_id}")

        # 标记为 in_progress
        db.update_task(
            task_id,
            status="in_progress",
            started_at=datetime.now().isoformat(),
            branch_name=f"working/{task_id}",
            worktree_path=worktree_path,
        )

        click.echo(f"[cyan]-> 执行任务 #{task_id}[/cyan]  {task['title']}")
        click.echo(f"  Agent: {task['agent']}  优先级: {task['priority']}")
        click.echo(f"  任务文件: {task_file}")

        if dry_run:
            click.echo("[dim]  [DryRun 模式，跳过实际执行][/dim]\n")
            db.update_task(task_id, status="backlog")
            executed += 1
            continue

        # 调用 dispatch
        try:
            exit_code, output = _run_dispatch(
                project=proj,
                task_file=task_file,
                agent_mode=task["agent"] or agent_mode,
                shell=shell_info,
                dry_run=dry_run,
            )
        except Exception as e:
            click.echo(f"[red]执行出错: {e}[/red]")
            db.update_task(
                task_id,
                status="backlog",
                error_message=str(e),
            )
            failed += 1
            continue

        # 解析 dispatch 结果
        # exit_code: 0=成功合并, 1=执行失败, 2=合并失败(已回退), 3=无变更
        if exit_code == 0:
            db.update_task(
                task_id,
                status="done",
                completed_at=datetime.now().isoformat(),
            )
            click.echo(f"[green][OK] 任务 #{task_id} 完成[/green]")
            if cleanup:
                _cleanup_worktree(project_path, worktree_path)
            # Webhook 通知
            notify_task_status(str(project_path), task_id, task["title"], "done")

        elif exit_code == 1:
            db.update_task(
                task_id,
                status="backlog",
                error_message="Agent 执行失败",
            )
            click.echo(f"[red][X] 任务 #{task_id} Agent 执行失败[/red]")
            failed += 1
            notify_task_status(str(project_path), task_id, task["title"], "failed", "Agent 执行失败")

        elif exit_code == 2:
            db.update_task(
                task_id,
                status="backlog",
                error_message="合并失败，已回退",
            )
            click.echo(f"[yellow][!] 任务 #{task_id} 合并失败，已回退[/yellow]")

        else:
            click.echo(f"[dim]任务 #{task_id} 退出码={exit_code}[/dim]")

        # 打印 dispatch 输出摘要（最后 10 行）
        output_lines = output.strip().split("\n")
        if output_lines[-10:]:
            click.echo("[dim]--- dispatch 输出 ---[/dim]")
            for line in output_lines[-10:]:
                if line.strip():
                    click.echo(f"  {line}")
        click.echo()

        executed += 1

        if once:
            break

        # 每轮间隔 3 秒
        time.sleep(3)

    # 汇总
    total = executed + failed
    click.echo(f"\n[dim]Run 完成: 执行 {executed}, 失败 {failed}[/dim]")
