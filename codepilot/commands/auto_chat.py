"""Interactive chat REPL for CodePilot.

Owns :func:`run_chat_session` and its helpers. Imported and re-exported by
:mod:`codepilot.commands.auto` so historical ``from codepilot.commands.auto import
run_chat_session`` / direct attribute access paths keep working.

Dependencies that tests monkeypatch on the shell module (``classify_intent``,
``answer_question_via_api``, ``run_requirement_workflow``, ``_project_config``,
``render_project_dashboard``, ``render_project_stats``) are looked up at call
time via ``codepilot.commands.auto``.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional

import click

from codepilot import __version__


def _shell():
    """Return the ``codepilot.commands.auto`` module for dynamic attribute lookup."""
    return sys.modules["codepilot.commands.auto"]


def _chat_help() -> str:
    return "\n".join(
        [
            "会话命令：",
            "  /help               查看帮助",
            "  /version            查看当前版本",
            "  /exit               退出会话",
            "  /status             查看任务看板（/status watch 实时刷新）",
            "  /stats              查看状态统计",
            "  /history            查看对话记录",
            "  /clear              清空对话历史",
            "  /project <name>     切换项目",
            "  /agent <name>       切换默认任务智能体（如 codex / claude / dual）",
            "  /execute on|off     切换默认是否自动执行",
            "  /plan               只规划下一条需求，不执行",
            "  /run                自动执行下一条需求",
            "",
            "任务管理：",
            "  /cancel <id ...>    取消任务（保留记录）",
            "  /resume <id ...>    恢复 cancelled/failed 任务到 backlog",
            "  /stop <id>          停止运行中的任务",
            "  /retry <id>         重试任务",
            "  /rm <id ...>        删除任务",
            "  /logs <id>          查看任务日志",
            "",
            "输入前缀（跳过自动分类）：",
            "  ? <文本>            当作问题直接回答，不建任务",
            "  ! <文本>            当作单任务，不拆分",
            "  # <文本>            当作需求，强制拆分",
            "",
            "会话内会自动记住上下文，连续提问无需重复说明。",
        ]
    )


def _parse_intent_prefix(text: str) -> tuple[Optional[str], str]:
    """Return (forced_intent, stripped_text). forced_intent ∈ {question,task,requirement} 或 None."""
    if not text:
        return None, text
    first, rest = text[0], text[1:].lstrip()
    if first == "?" and rest:
        return "question", rest
    if first == "!" and rest:
        return "task", rest
    if first == "#" and rest:
        return "requirement", rest
    return None, text


class _Spinner:
    """Simple inline spinner for long-running operations."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "思考中"):
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stderr.write(f"\r  {frame} {self._message}...")
            sys.stderr.flush()
            i += 1
            self._stop.wait(0.1)


def _start_chat_ui(port: int = 8766):
    """Start Web UI in a background daemon thread for chat mode."""
    from codepilot.output import echo

    try:
        from codepilot.webui import start_ui_server
        server = start_ui_server(host="127.0.0.1", port=port, open_browser=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        echo(f"[dim]Web UI 已启动: http://127.0.0.1:{port}/[/dim]")
        return server
    except OSError:
        return None
    except Exception:
        return None


def run_chat_session(
    *,
    project: Optional[str] = None,
    planner: Optional[str] = None,
    task_agent: Optional[str] = None,
    execute: Optional[bool] = None,
    executor: Optional[str] = None,
    auto_commit: Optional[bool] = None,
    max_tasks: int = 0,
    max_retries: int = 0,
    enable_ui: bool = True,
    ui_port: int = 8766,
) -> None:
    """Run a simple REPL that accepts plain-text requirements."""
    from codepilot.output import echo, safe

    shell = _shell()
    project_info = shell.resolve_project_for_prompt(project)
    effective = shell._resolve_effective_options(
        project_info,
        planner=planner,
        executor=executor,
        auto_commit=auto_commit,
        max_tasks=max_tasks,
        max_retries=max_retries,
    )
    default_execute = effective["auto_execute"] if execute is None else execute
    default_agent = shell._resolve_task_agent(project_info, task_agent, effective["executor"])

    ui_server = None
    if enable_ui:
        ui_server = _start_chat_ui(ui_port)

    chat_history: list[dict] = []
    # Multi-turn requirement clarification state. When AI asks for more info we
    # stash the original title + accumulated Q/A here, and the next user input
    # is treated as the answer to the most-recent batch of questions.
    pending_clarification: Optional[dict] = None

    echo(
        f"[cyan]CodePilot Chat[/cyan]  项目: {project_info['name']}  "
        f"planner={effective['planner']} executor={effective['executor']} agent={default_agent}"
    )
    echo("[dim]直接输入文本即可。问题会直接回答，需求会自动规划执行。[/dim]")
    echo("[dim]输入 /help 查看命令，/history 查看对话记录。[/dim]")
    echo()

    def _shutdown_ui():
        if ui_server:
            ui_server.shutdown()
            ui_server.server_close()

    while True:
        try:
            raw = click.prompt("codepilot", prompt_suffix="> ", default="", show_default=False)
        except (EOFError, KeyboardInterrupt):
            echo()
            _shutdown_ui()
            echo("[dim]会话已结束[/dim]")
            return

        text = raw.strip()
        if not text:
            continue

        if text.startswith("/"):
            parts = text.split()
            cmd = parts[0].lower()

            if cmd == "/exit":
                _shutdown_ui()
                echo("[dim]会话已结束[/dim]")
                return
            if cmd == "/help":
                click.echo(_chat_help())
                continue
            if cmd == "/version":
                click.echo(f"CodePilot {__version__}")
                continue
            if cmd == "/status":
                watch = len(parts) > 1 and parts[1].lower() in ("watch", "live", "-w")
                if watch:
                    echo("[dim]实时刷新中，按 Ctrl+C 停止...[/dim]")
                try:
                    while True:
                        if watch:
                            click.clear()
                        shell.render_project_dashboard(
                            project_info["name"],
                            verbose=False,
                            include_done=False,
                            title=f"任务面板  {project_info['name']}",
                        )
                        if not watch:
                            break
                        time.sleep(3)
                except KeyboardInterrupt:
                    if watch:
                        echo("\n[dim]已停止刷新[/dim]")
                continue
            if cmd == "/stats":
                shell.render_project_stats(project_info["name"], title=f"状态统计  {project_info['name']}")
                continue
            if cmd == "/project":
                if len(parts) < 2:
                    echo("[yellow]用法: /project <name>[/yellow]")
                    continue
                project_info = shell.resolve_project_for_prompt(parts[1])
                effective = shell._resolve_effective_options(
                    project_info,
                    planner=planner,
                    executor=executor,
                    auto_commit=auto_commit,
                    max_tasks=max_tasks,
                    max_retries=max_retries,
                )
                default_agent = shell._resolve_task_agent(project_info, default_agent, effective["executor"])
                echo(f"[green][OK] 已切换项目[/green] {project_info['name']}")
                continue
            if cmd == "/agent":
                if len(parts) < 2:
                    echo("[yellow]用法: /agent <name>[/yellow]")
                    continue
                try:
                    default_agent = shell._resolve_task_agent(project_info, parts[1], effective["executor"])
                except click.ClickException as exc:
                    echo(f"[red]{exc.format_message()}[/red]")
                    continue
                echo(f"[green][OK] 默认任务智能体已设置为 {default_agent}[/green]")
                continue
            if cmd == "/execute":
                if len(parts) < 2 or parts[1].lower() not in {"on", "off"}:
                    echo("[yellow]用法: /execute on|off[/yellow]")
                    continue
                default_execute = parts[1].lower() == "on"
                echo(f"[green][OK] 自动执行已设置为 {default_execute}[/green]")
                continue
            if cmd == "/plan":
                default_execute = False
                echo("[green][OK] 下一条需求将只规划不执行[/green]")
                continue
            if cmd == "/run":
                default_execute = True
                echo("[green][OK] 下一条需求将自动执行[/green]")
                continue

            if cmd == "/history":
                if not chat_history:
                    echo("[dim]暂无对话记录[/dim]")
                else:
                    for i, turn in enumerate(chat_history, 1):
                        intent_tag = turn.get("intent", "?")
                        echo(f"[dim]#{i}[/dim] [{intent_tag}] {turn['user'][:80]}")
                        if turn.get("assistant"):
                            click.echo(f"  → {turn['assistant'][:120]}")
                continue
            if cmd == "/clear":
                chat_history.clear()
                if pending_clarification:
                    pending_clarification = None
                    echo("[green]对话历史已清空，当前待澄清的需求也已放弃[/green]")
                else:
                    echo("[green]对话历史已清空[/green]")
                continue

            if cmd in ("/cancel", "/resume", "/retry", "/rm", "/stop", "/logs"):
                ids = [int(x) for x in parts[1:] if x.isdigit()]
                if not ids:
                    echo(f"[yellow]用法: {cmd} <task_id ...>[/yellow]")
                    continue
                from codepilot.commands import tasks as tasks_cmd_mod
                from click.testing import CliRunner as _InlineRunner
                cli_cmd_map = {
                    "/cancel": tasks_cmd_mod.cancel,
                    "/resume": tasks_cmd_mod.resume,
                    "/retry": tasks_cmd_mod.retry,
                    "/rm": tasks_cmd_mod.rm,
                    "/stop": tasks_cmd_mod.stop,
                    "/logs": tasks_cmd_mod.logs,
                }
                target_cmd = cli_cmd_map[cmd]
                args = [str(i) for i in ids]
                if cmd in ("/rm", "/cancel"):
                    args.append("-f") if cmd == "/rm" else None
                _InlineRunner().invoke(target_cmd, args)
                continue

            echo("[yellow]未知会话命令[/yellow]")
            click.echo(_chat_help())
            continue

        forced_intent, payload_text = _parse_intent_prefix(text)

        # ── Multi-turn clarification: user is answering outstanding questions ──
        if pending_clarification and not forced_intent and not text.startswith("?"):
            answer_text = payload_text
            qa_history = list(pending_clarification.get("qa_history", []))
            last_questions = pending_clarification.get("last_questions") or []
            # Attach the user's answer to the most-recent round of questions.
            if last_questions:
                merged_q = " | ".join(last_questions)
                qa_history.append({"question": merged_q, "answer": answer_text})
            else:
                qa_history.append({"question": "", "answer": answer_text})

            spinner = _Spinner("正在评估补充信息")
            spinner.__enter__()
            try:
                assessment = shell.clarify_requirement(
                    pending_clarification["original_title"],
                    project_info=project_info,
                    qa_history=qa_history,
                    planner=effective["planner"],
                )
            finally:
                spinner.__exit__(None, None, None)

            if assessment.get("status") == "needs_clarification":
                questions = assessment.get("questions") or []
                pending_clarification = {
                    "original_title": pending_clarification["original_title"],
                    "qa_history": qa_history,
                    "last_questions": questions,
                    "intent": pending_clarification.get("intent", "requirement"),
                }
                echo("[cyan]还需要再澄清一下：[/cyan]")
                for i, q in enumerate(questions, 1):
                    click.echo(f"  {i}. {q}")
                chat_history.append({
                    "user": answer_text,
                    "assistant": "继续澄清：" + " / ".join(questions),
                    "intent": "clarify",
                })
                click.echo()
                continue

            # Ready — take refined title forward into planning.
            refined = assessment.get("refined_title") or pending_clarification["original_title"]
            pending_intent = pending_clarification.get("intent", "requirement")
            pending_clarification = None
            echo(f"[green][OK] 已澄清需求：{refined}[/green]")

            try:
                max_tasks_override = 1 if pending_intent == "task" else effective["max_tasks"]
                shell.run_requirement_workflow(
                    project_info=project_info,
                    title=refined,
                    planner=effective["planner"],
                    task_agent=default_agent,
                    execute=default_execute,
                    executor=effective["executor"],
                    auto_commit=effective["auto_commit"],
                    max_tasks=max_tasks_override,
                    max_retries=effective["max_retries"],
                    quiet=True,
                )
                chat_history.append({
                    "user": answer_text,
                    "assistant": "需求已规划并执行",
                    "intent": pending_intent,
                })
            except click.ClickException as exc:
                echo(f"[red]{safe(exc.format_message())}[/red]")
                chat_history.append({
                    "user": answer_text,
                    "assistant": f"错误: {exc.format_message()}",
                    "intent": pending_intent,
                })
            except Exception as exc:
                echo(f"[red]{safe(exc)}[/red]")
                chat_history.append({
                    "user": answer_text,
                    "assistant": f"错误: {exc}",
                    "intent": pending_intent,
                })
            click.echo()
            continue

        spinner = _Spinner("处理中")
        spinner.__enter__()

        intent = forced_intent
        if intent is None:
            cfg = shell._project_config(project_info)
            classifier_cfg = getattr(cfg, "classifier", None)
            api_key = None
            base_url = None
            classifier_provider = ""
            classifier_model = ""
            classifier_timeout = 30
            if classifier_cfg and classifier_cfg.enabled:
                classifier_provider = classifier_cfg.provider or ""
                classifier_model = classifier_cfg.model or ""
                classifier_timeout = classifier_cfg.timeout or 30
                if classifier_provider:
                    api_key = cfg.get_provider_api_key(classifier_provider)
                    provider_cfg = cfg.providers.get(classifier_provider)
                    base_url = provider_cfg.base_url if provider_cfg else None
            try:
                result = shell.classify_intent(
                    payload_text,
                    project_path=project_info["path"],
                    classifier_provider=classifier_provider,
                    classifier_model=classifier_model,
                    timeout=classifier_timeout,
                    api_key=api_key,
                    base_url=base_url,
                )
                intent = result["intent"]
            except Exception:
                intent = "requirement"

        intent_labels = {"question": "正在思考", "task": "正在执行", "requirement": "正在规划", "command": "处理中"}
        spinner._message = intent_labels.get(intent, "处理中")

        assistant_response = ""
        try:
            if intent == "command":
                spinner.__exit__(None, None, None)
                assistant_response = "请使用对应的 CLI 命令操作"
                echo(
                    "[yellow]这看起来是在调用 codepilot 自身命令，请直接用下面的入口：[/yellow]"
                )
                click.echo(
                    "  状态总览:  codepilot status -p <项目> -v\n"
                    "  任务日志:  codepilot logs <task_id>\n"
                    "  重试任务:  codepilot retry <task_id>\n"
                    "  停止任务:  codepilot stop <task_id>\n"
                    "  触发巡检:  codepilot inspect -p <项目>\n"
                    "  发布打包:  codepilot release prepare --version <版本>"
                )
                click.echo()
            elif intent == "question":
                cfg = shell._project_config(project_info)
                classifier_cfg = getattr(cfg, "classifier", None)
                provider_key = classifier_cfg.provider if classifier_cfg else ""
                api_key = cfg.get_provider_api_key(provider_key) if provider_key else None
                provider_cfg = cfg.providers.get(provider_key) if provider_key else None
                base_url = provider_cfg.base_url if provider_cfg else None
                answer = shell.answer_question_via_api(
                    provider_key=provider_key,
                    question=payload_text,
                    project_path=project_info["path"],
                    model_override=classifier_cfg.model if classifier_cfg else "",
                    api_key=api_key,
                    base_url=base_url,
                    history=chat_history,
                )
                spinner.__exit__(None, None, None)
                if answer:
                    click.echo(answer)
                    assistant_response = answer
                else:
                    echo("[yellow]未获得回答[/yellow]")
            elif intent in ("task", "requirement"):
                # ── Step 1: clarify if the requirement looks vague ──
                spinner._message = "正在评估需求完整度"
                assessment = shell.clarify_requirement(
                    payload_text,
                    project_info=project_info,
                    qa_history=[],
                    planner=effective["planner"],
                )
                spinner.__exit__(None, None, None)

                if assessment.get("status") == "needs_clarification":
                    questions = assessment.get("questions") or []
                    pending_clarification = {
                        "original_title": payload_text,
                        "qa_history": [],
                        "last_questions": questions,
                        "intent": intent,
                    }
                    echo("[cyan]为了更好地规划，我想先确认几个点：[/cyan]")
                    for i, q in enumerate(questions, 1):
                        click.echo(f"  {i}. {q}")
                    echo("[dim]请直接回复你的答案（可以一次性全写）。输入 /clear 放弃此需求。[/dim]")
                    assistant_response = "请求澄清：" + " / ".join(questions)
                else:
                    refined = assessment.get("refined_title") or payload_text
                    max_tasks_override = 1 if intent == "task" else effective["max_tasks"]
                    shell.run_requirement_workflow(
                        project_info=project_info,
                        title=refined,
                        planner=effective["planner"],
                        task_agent=default_agent,
                        execute=default_execute,
                        executor=effective["executor"],
                        auto_commit=effective["auto_commit"],
                        max_tasks=max_tasks_override,
                        max_retries=effective["max_retries"],
                        quiet=True,
                    )
                    assistant_response = (
                        "任务已创建并执行" if intent == "task" else "需求已规划"
                    )
        except click.ClickException as exc:
            spinner.__exit__(None, None, None)
            echo(f"[red]{safe(exc.format_message())}[/red]")
            assistant_response = f"错误: {exc.format_message()}"
        except Exception as exc:
            spinner.__exit__(None, None, None)
            echo(f"[red]{safe(exc)}[/red]")
            assistant_response = f"错误: {exc}"

        chat_history.append({
            "user": payload_text,
            "assistant": assistant_response,
            "intent": intent or "unknown",
        })
        click.echo()
