from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codepilot import db
from codepilot.cli import main
from codepilot.commands import inspect as inspect_cmd


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _register_project(tmp_path, monkeypatch, name: str, agents_toml: str) -> Path:
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()

    project_path = tmp_path / name
    _write(project_path / "AGENTS.toml", agents_toml)
    db.register_project(name, str(project_path))
    return project_path


def _stub_inspection(monkeypatch, captured: dict) -> None:
    def fake_run_inspection(project_info, **kwargs):
        captured[project_info["name"]] = kwargs["planner"]
        return {
            "project": project_info["name"],
            "candidates_total": 0,
            "created": [],
            "skipped": [],
            "auto_execute": kwargs.get("auto_execute", False),
        }

    monkeypatch.setattr(inspect_cmd, "run_inspection", fake_run_inspection)


def test_inspect_prefers_explicit_planner(tmp_path, monkeypatch):
    _register_project(
        tmp_path,
        monkeypatch,
        "demo",
        """
[project]
name = "demo"

[agents]
planner = "codex"

[inspect]
planner = "claude"
""".strip(),
    )
    captured = {}
    _stub_inspection(monkeypatch, captured)

    result = CliRunner().invoke(
        main,
        ["inspect", "-p", "demo", "--planner", "codex", "--once", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert captured["demo"] == "codex"


def test_inspect_uses_inspect_then_agents_planner(tmp_path, monkeypatch):
    _register_project(
        tmp_path,
        monkeypatch,
        "inspect-first",
        """
[project]
name = "inspect-first"

[agents]
planner = "codex"

[inspect]
planner = "claude"
""".strip(),
    )
    _register_project(
        tmp_path,
        monkeypatch,
        "agents-fallback",
        """
[project]
name = "agents-fallback"

[agents]
planner = "claude"
""".strip(),
    )
    captured = {}
    _stub_inspection(monkeypatch, captured)
    runner = CliRunner()

    first = runner.invoke(
        main,
        ["inspect", "-p", "inspect-first", "--once", "--dry-run", "--json"],
    )
    second = runner.invoke(
        main,
        ["inspect", "-p", "agents-fallback", "--once", "--dry-run", "--json"],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert captured["inspect-first"] == "claude"
    assert captured["agents-fallback"] == "claude"


def test_inspect_falls_back_to_codex_without_planner_config(tmp_path, monkeypatch):
    _register_project(
        tmp_path,
        monkeypatch,
        "demo",
        """
[project]
name = "demo"
""".strip(),
    )
    captured = {}
    _stub_inspection(monkeypatch, captured)

    result = CliRunner().invoke(
        main,
        ["inspect", "-p", "demo", "--once", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert captured["demo"] == "codex"
