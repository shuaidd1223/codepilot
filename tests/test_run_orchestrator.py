from __future__ import annotations

from pathlib import Path

from codepilot import db
from codepilot import webui as webui_mod
from codepilot.commands import run as run_cmd


def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0


def _register_project_with_config(tmp_path: Path) -> Path:
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_file = project_path / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
base_branch = "dev"

[automation]
per_task_branch = false
task_workspace = "branch"
""".strip(),
        encoding="utf-8",
    )
    db.register_project("demo", str(project_path), base_branch="dev", config_file=str(config_file))
    return project_path


def test_run_backlog_quiet_mode_skips_dashboard_render_on_success(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_config(tmp_path)
    db.create_task("demo", "quiet success", agent="claude", max_retries=2)

    render_calls: list[dict] = []
    monkeypatch.setattr(
        run_cmd,
        "render_project_dashboard",
        lambda *args, **kwargs: render_calls.append({"args": args, "kwargs": kwargs}),
    )
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=0,
            output="ok",
            summary="done",
            executor="builtin",
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)

    assert stats["done"] == 1
    assert render_calls == []


def test_run_backlog_quiet_mode_skips_dashboard_render_on_preflight_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_config(tmp_path)
    task = db.create_task("demo", "quiet preflight", agent="dual", max_retries=2)

    render_calls: list[dict] = []
    monkeypatch.setattr(
        run_cmd,
        "render_project_dashboard",
        lambda *args, **kwargs: render_calls.append({"args": args, "kwargs": kwargs}),
    )
    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "preflight blocked")

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)
    current = db.get_task(task["id"])

    assert stats["processed"] == 1
    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert "preflight blocked" in (current["error_message"] or "")
    assert render_calls == []
