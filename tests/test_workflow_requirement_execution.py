from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from codepilot.storage import database as db
from codepilot.ai_support import service as ai_mod
from codepilot.commands import auto as auto_cmd
from tests.workflow_testkit import init_test_db as _init_test_db


def test_run_requirement_workflow_executes_without_retry_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    project = db.get_project("demo")
    captured = {}

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: {
            "summary": "ok",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                }
            ],
        },
    )
    monkeypatch.setattr(auto_cmd, "render_project_dashboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        auto_cmd,
        "run_backlog",
        lambda *args, **kwargs: captured.update(kwargs) or {"processed": 1, "done": 1, "failed": 0, "requeued": 0},
    )

    auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让它直接执行",
        planner="codex",
        execute=True,
        executor="builtin",
        auto_commit=False,
    )

    assert captured["retry_on_failure"] is False


def test_generate_task_breakdown_uses_codex_planner(monkeypatch):
    captured = {}

    def fake_run_codex_schema_prompt(prompt, schema, *, project_path="", config_ref=None, timeout=240):
        captured["project_path"] = project_path
        captured["config_ref"] = config_ref
        captured["timeout"] = timeout
        return {
            "summary": "ok",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                }
            ],
        }

    monkeypatch.setattr("codepilot.ai_support.service._run_codex_schema_prompt", fake_run_codex_schema_prompt)

    breakdown = auto_cmd.generate_task_breakdown(
        title="实现一个自动重试机制",
        project_path="D:/demo",
        planner="codex",
        max_tasks=3,
    )

    assert breakdown["tasks"][0]["title"] == "step 1"
    assert captured["project_path"] == "D:/demo"
    assert captured["config_ref"] is None


def test_run_requirement_workflow_routes_breakdown_through_parser(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")
    captured = {}

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: (
            captured.setdefault("planner_kwargs", kwargs),
            """
            {
              "summary": "raw",
              "tasks": [
                {
                  "title": "raw step",
                  "priority": "P2",
                  "goal": "g",
                  "acceptance_criteria": ["a"],
                  "builder_notes": [],
                  "reviewer_notes": [],
                  "files": [],
                  "notes": []
                }
              ]
            }
            """,
        )[1],
    )

    def fake_parse(breakdown, *, title, max_tasks, existing_tasks=None):
        captured["breakdown"] = breakdown
        captured["title"] = title
        captured["max_tasks"] = max_tasks
        return {
            "summary": "raw",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "parsed step",
                    "priority": "P1",
                    "goal": "g",
                    "acceptance_criteria": ["a"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                }
            ],
        }

    monkeypatch.setattr(auto_cmd, "parse_automation_planner_result", fake_parse)

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让 workflow 入口走统一解析",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert captured["planner_kwargs"]["parse_result"] is False
    assert captured["title"] == "让 workflow 入口走统一解析"
    assert captured["max_tasks"] == 5
    assert isinstance(captured["breakdown"], str)
    assert payload["tasks"][0]["title"] == "parsed step"


def test_run_requirement_workflow_supports_legacy_breakdown_stub_without_parse_flag(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")
    captured = {}

    def legacy_breakdown(*, title, project_path, planner, max_tasks, config_ref, two_stage, existing_tasks):
        captured["planner"] = planner
        captured["two_stage"] = two_stage
        return {
            "summary": "legacy",
            "tasks": [
                {
                    "title": title,
                    "priority": "P2",
                    "goal": "g",
                    "acceptance_criteria": ["a"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                }
            ],
        }

    monkeypatch.setattr(auto_cmd, "generate_task_breakdown", legacy_breakdown)
    monkeypatch.setattr(
        auto_cmd,
        "parse_automation_planner_result",
        lambda breakdown, **kwargs: {
            **breakdown,
            "complexity": "simple",
            "should_split": False,
        },
    )

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="兼容旧版 breakdown stub",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert captured["planner"] == "codex"
    assert captured["two_stage"] is True
    assert payload["tasks"][0]["title"] == "兼容旧版 breakdown stub"


def test_run_requirement_workflow_wraps_parser_error_in_click_exception(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: {"summary": "raw", "tasks": [{"title": "raw step", "priority": "P2"}]},
    )
    monkeypatch.setattr(
        auto_cmd,
        "parse_automation_planner_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("planner parse failed")),
    )

    try:
        auto_cmd.run_requirement_workflow(
            project_info=project,
            title="入口解析失败要走统一异常语义",
            planner="codex",
            execute=False,
            executor="builtin",
            auto_commit=False,
        )
    except Exception as exc:
        assert isinstance(exc, click.ClickException)
        assert "planner parse failed" in exc.format_message()
    else:
        raise AssertionError("expected ClickException")


def test_run_codex_schema_prompt_kills_process_and_raises_runtime_error_on_interrupt(monkeypatch):
    class _FakeProvider:
        name = "Codex"

        def find_executable(self):
            return Path("codex")

    class _FakeStdin:
        def write(self, _text):
            return None

        def close(self):
            return None

    class _FakeStream:
        def read(self):
            return ""

        def __iter__(self):
            return iter(())

    class _FakeProcess:
        def __init__(self):
            self.pid = 4321
            self.stdin = _FakeStdin()
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

        def poll(self):
            raise KeyboardInterrupt()

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

    fake_process = _FakeProcess()
    killed: list[int] = []

    monkeypatch.setattr(ai_mod, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(ai_mod, "resolve_cli_provider", lambda *args, **kwargs: _FakeProvider())
    monkeypatch.setattr(ai_mod.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(ai_mod, "_kill_process_tree", lambda pid: killed.append(int(pid)))

    try:
        ai_mod._run_codex_schema_prompt("prompt", {"type": "object"})
    except RuntimeError as exc:
        assert "中断" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert killed == [4321]
    assert fake_process.returncode == -9


def test_run_claude_schema_prompt_kills_process_and_raises_runtime_error_on_interrupt(monkeypatch):
    class _FakeProvider:
        name = "Claude CLI"

        def find_executable(self):
            return Path("claude")

    class _FakeStream:
        def read(self):
            return ""

        def __iter__(self):
            return iter(())

    class _FakeProcess:
        def __init__(self):
            self.pid = 8765
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

        def poll(self):
            raise KeyboardInterrupt()

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

    fake_process = _FakeProcess()
    killed: list[int] = []

    monkeypatch.setattr(ai_mod, "resolve_cli_provider", lambda *args, **kwargs: _FakeProvider())
    monkeypatch.setattr(ai_mod.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(ai_mod, "_kill_process_tree", lambda pid: killed.append(int(pid)))

    try:
        ai_mod._run_claude_schema_prompt("prompt", {"type": "object"}, planner="claude")
    except RuntimeError as exc:
        assert "中断" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert killed == [8765]
    assert fake_process.returncode == -9


def test_run_claude_schema_prompt_surfaces_stderr_hint_with_gbk_fallback(monkeypatch):
    class _FakeProvider:
        name = "Claude Code"

        def find_executable(self):
            return Path("claude")

    class _FakeBytesStream:
        def __init__(self, *, lines=None, blob=b""):
            self._lines = list(lines or [])
            self._blob = blob

        def __iter__(self):
            return iter(self._lines)

        def read(self):
            return self._blob

    class _FakeProcess:
        def __init__(self):
            self.pid = 9527
            self.stdout = _FakeBytesStream(blob=b"")
            self.stderr = _FakeBytesStream(lines=["系统繁忙，请稍后重试".encode("gb18030") + b"\n"])
            self.returncode = 1

        def poll(self):
            return self.returncode

    fake_process = _FakeProcess()

    monkeypatch.setattr(ai_mod, "resolve_cli_provider", lambda *args, **kwargs: _FakeProvider())
    monkeypatch.setattr(ai_mod.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    with pytest.raises(RuntimeError) as exc:
        ai_mod._run_claude_schema_prompt("prompt", {"type": "object"}, planner="claude")

    assert "系统繁忙" in str(exc.value)


def test_run_codex_schema_prompt_surfaces_stderr_hint_with_gbk_fallback(monkeypatch):
    class _FakeProvider:
        name = "Codex"

        def find_executable(self):
            return Path("codex")

    class _FakeStdin:
        def write(self, _data):
            return None

        def close(self):
            return None

    class _FakeBytesStream:
        def __init__(self, *, lines=None):
            self._lines = list(lines or [])

        def __iter__(self):
            return iter(self._lines)

    class _FakeProcess:
        def __init__(self):
            self.pid = 2468
            self.stdin = _FakeStdin()
            self.stdout = _FakeBytesStream(lines=[])
            self.stderr = _FakeBytesStream(lines=["系统繁忙，请稍后重试".encode("gb18030") + b"\n"])
            self.returncode = 1

        def poll(self):
            return self.returncode

    fake_process = _FakeProcess()

    monkeypatch.setattr(ai_mod, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(ai_mod, "resolve_cli_provider", lambda *args, **kwargs: _FakeProvider())
    monkeypatch.setattr(ai_mod.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    with pytest.raises(RuntimeError) as exc:
        ai_mod._run_codex_schema_prompt("prompt", {"type": "object"})

    assert "系统繁忙" in str(exc.value)


def test_run_requirement_workflow_falls_back_to_single_codex_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Codex planning timeout")),
    )

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让工具自己优化自己",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert payload["complexity"] == "simple"
    assert payload["should_split"] is False
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["agent"] == "codex"


def test_run_requirement_workflow_retries_claude_then_falls_back_to_codex(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(auto_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    planner_calls: list[str] = []

    def _mock_breakdown(**kwargs):
        planner = kwargs.get("planner") or ""
        planner_calls.append(planner)
        if planner == "claude":
            raise RuntimeError("Claude Code 没有成功完成任务拆分。请检查 Claude CLI 当前是否可用。")
        return {
            "summary": "fallback to codex",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "补齐账单中心页面",
                    "priority": "P1",
                    "goal": "补齐账单中心核心能力",
                    "acceptance_criteria": ["页面可展示余额与余额记录", "支持充值入口"],
                    "builder_notes": ["优先复用现有余额模块"],
                    "reviewer_notes": ["检查充值流程和余额一致性"],
                    "files": ["src/billing/page.tsx"],
                    "notes": [],
                    "evidence": "recon: src/billing 目录存在但无入口页面",
                }
            ],
        }

    monkeypatch.setattr(auto_cmd, "generate_task_breakdown", _mock_breakdown)

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="新建账单中心并支持充值",
        planner="claude",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert planner_calls == ["claude", "claude", "codex"]
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["agent"] == "codex"


def test_run_requirement_workflow_uses_registered_config_file_for_provider_resolution(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    config_root = tmp_path / "config-root"
    project_path.mkdir()
    config_root.mkdir()
    config_file = config_root / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
default_mode = "codex"
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), default_mode="codex", config_file=str(config_file))
    project = db.get_project("demo")
    captured = {}

    def fake_check_provider(agent, project_path=None):
        captured["provider_path"] = project_path
        return True, f"ok:{agent}"

    monkeypatch.setattr(auto_cmd, "check_provider_availability", fake_check_provider)
    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: captured.update({"config_ref": kwargs.get("config_ref")}) or {
            "summary": "ok",
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                }
            ],
        },
    )

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让工具自己优化自己",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert payload["tasks"][0]["agent"] == "codex"
    assert captured["provider_path"] == str(config_file)
    assert captured["config_ref"] == str(config_file)


def test_run_requirement_workflow_does_not_fallback_on_non_timeout_codex_error(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("当前无法使用 Codex，因为本机没有找到 `codex` 命令。")),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        try:
            auto_cmd.run_requirement_workflow(
                project_info=project,
                title="让工具自己优化自己",
                planner="codex",
                execute=False,
                executor="builtin",
                auto_commit=False,
            )
        except Exception as exc:
            assert isinstance(exc, click.ClickException)
            assert "当前无法使用 Codex" in exc.format_message()
        else:
            raise AssertionError("expected ClickException")

