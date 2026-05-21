from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.ai_support.agent_support import ai_guide_markdown, command_manifest
from codepilot.binary_support import manager as binary_mod
from codepilot.binary_support import paths as binary_paths_mod
from codepilot.storage import database as db
from codepilot.ai_support import service as ai_mod
from codepilot.ai_support.clarification_protocol import (
    build_clarification_input_summary,
    normalize_clarification_answers,
)
from codepilot.core import progress_bus
from codepilot.gateway.service import GatewayResponse
from codepilot.core import runtime as runtime_mod
from codepilot.webapp import server as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.core.config import load_project_config
from tests.workflow_testkit import init_test_db as _init_test_db


def _q(
    text: str,
    *,
    qid: str = "q1",
    qtype: str = "text",
    options: list[tuple[str, str]] | None = None,
    allow_free_text: bool = False,
) -> dict:
    return {
        "id": qid,
        "type": qtype,
        "text": text,
        "options": [{"id": option_id, "label": label} for option_id, label in (options or [])],
        "allow_free_text": allow_free_text,
    }


def _a(question: dict, answer: str) -> dict:
    return {
        "question_id": question["id"],
        "question_text": question["text"],
        "type": question["type"],
        "selected_option_ids": [],
        "selected_option_labels": [],
        "free_text": answer,
        "answer_text": answer,
    }


def _h(question: dict, answer: str) -> dict:
    return {
        "questions": [question],
        "answers": [_a(question, answer)],
        "answer": f"{question['text']}：{answer}",
    }


def test_clarify_requirement_passes_config_ref_to_assess_requirement(tmp_path, monkeypatch):
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
[automation]
planner = "claude"
""".strip(),
        encoding="utf-8",
    )

    project_info = db.register_project("demo", str(project_path), config_file=str(config_file))

    result = auto_cmd.clarify_requirement(
        "优化一下",
        project_info=project_info,
        planner="claude",
    )

    # 旧的 clarify 模式已移除，直接返回 ready
    assert result["status"] == "ready"
    assert result["source"] == "passthrough"
    assert result["refined_title"] == "优化一下"
    assert result["skip_reason"] == "delegated_to_ai_agent"  # 默认配置下=AI 接管


def test_assess_requirement_for_planning_uses_shared_input_builder(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project_info = db.register_project("demo", str(project_path))
    seen: dict[str, object] = {}

    def _fake_clarify(title, *, qa_history=None, **kwargs):
        seen["title"] = title
        seen["qa_history"] = qa_history
        return {"status": "ready", "refined_title": "  细化需求  ", "qa_history": qa_history}

    monkeypatch.setattr(auto_cmd, "clarify_requirement", _fake_clarify)

    result = auto_cmd.assess_requirement_for_planning(
        "  先做 Web UI  ",
        project_info=project_info,
        planner="codex",
        original_title="  优化一下 ",
        qa_history=[_h(_q("Q1", qid="prev"), "A1")],
        last_questions=[_q("先做哪块?", qid="scope")],
    )

    assert seen["title"] == "优化一下"
    assert seen["qa_history"] == [
        _h(_q("Q1", qid="prev"), "A1"),
        _h(_q("先做哪块?", qid="scope"), "先做 Web UI"),
    ]
    assert result["seed_title"] == "优化一下"
    assert result["refined_title"] == "细化需求"


def test_append_clarification_answer_normalizes_history_rows():
    qa_history = [_h(_q("Q1", qid="prev"), "A1")]
    merged = auto_cmd.append_clarification_answer(
        qa_history,
        answer="  继续补充  ",
        questions=[{}, _q("先确认范围", qid="scope"), ""],
    )

    assert merged == [
        _h(_q("Q1", qid="prev"), "A1"),
        _h(_q("先确认范围", qid="scope"), "继续补充"),
    ]
    # Original input remains untouched.
    assert qa_history == [_h(_q("Q1", qid="prev"), "A1")]


def test_protocol_single_free_text_override_clears_selected_option():
    question = _q(
        "先覆盖哪个入口?",
        qid="entry",
        qtype="single",
        options=[("web", "Web UI"), ("cli", "CLI")],
        allow_free_text=True,
    )

    answers = normalize_clarification_answers(
        [question],
        raw_answers=[{
            "question_id": "entry",
            "selected_option_ids": ["web"],
            "free_text": "桌面端集成",
        }],
    )

    assert answers == [{
        "question_id": "entry",
        "question_text": "先覆盖哪个入口?",
        "type": "single",
        "selected_option_ids": [],
        "selected_option_labels": [],
        "free_text": "桌面端集成",
        "answer_text": "其他：桌面端集成",
    }]


def test_build_clarification_input_summary_uses_question_schema_for_raw_answers():
    question = _q(
        "先覆盖哪个入口?",
        qid="entry",
        qtype="single",
        options=[("web", "Web UI"), ("cli", "CLI")],
        allow_free_text=True,
    )

    summary = build_clarification_input_summary(
        [question],
        raw_answers=[{"question_id": "entry", "selected_option_ids": ["web"]}],
    )

    assert summary == "先覆盖哪个入口?：Web UI"


def test_build_clarification_state_normalizes_payload():
    state = auto_cmd.build_clarification_state(
        original_title="  优化 一下  ",
        qa_history=[
            _h(_q("  Q1  ", qid="prev"), "  A1  "),
            {"questions": [_q(" ", qid="blank")], "answer": " "},
            "invalid",
        ],
        last_questions=[_q(" 先做哪块? ", qid="scope"), ""],
        intent=" TASK ",
    )

    assert state == {
        "original_title": "优化 一下",
        "qa_history": [_h(_q("Q1", qid="prev"), "A1")],
        "last_questions": [_q("先做哪块?", qid="scope")],
        "intent": "task",
    }


def test_clarification_state_helpers_keep_intent_across_rounds():
    pending = auto_cmd.build_clarification_state(
        original_title="优化一下",
        qa_history=[],
        last_questions=[_q("先做哪块?", qid="scope")],
        intent="task",
    )

    with_answer = auto_cmd.append_clarification_answer_to_state(
        pending,
        answer=" 先做 Web UI ",
    )
    assert with_answer["qa_history"] == [
        _h(_q("先做哪块?", qid="scope"), "先做 Web UI")
    ]

    next_state = auto_cmd.clarification_state_from_assessment(
        assessment={
            "status": "needs_clarification",
            "questions": [_q(" 目标是啥? ", qid="goal")],
            "qa_history": with_answer["qa_history"],
        },
        seed_title="ignored",
        previous_state=with_answer,
    )
    assert next_state == {
        "original_title": "优化一下",
        "qa_history": [_h(_q("先做哪块?", qid="scope"), "先做 Web UI")],
        "last_questions": [_q("目标是啥?", qid="goal")],
        "intent": "task",
    }
    assert auto_cmd.clarification_state_from_assessment(
        assessment={"status": "ready"},
        seed_title="优化一下",
        previous_state=with_answer,
    ) is None


def test_resolve_project_for_prompt_uses_current_directory(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    project = auto_cmd.resolve_project_for_prompt()

    assert project["name"] == "demo"
    assert project["path"] == str(project_path)


def test_init_dot_defaults_project_name_to_current_directory(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "current-project"
    project_path.mkdir()
    monkeypatch.chdir(project_path)

    result = CliRunner().invoke(main, ["init", "."])

    assert result.exit_code == 0, result.output
    project = db.get_project("current-project")
    assert project is not None
    assert project["name"] == "current-project"
    assert project["path"] == str(project_path.resolve())
    content = (project_path / "AGENTS.toml").read_text(encoding="utf-8")
    assert 'name = "current-project"' in content
    assert "app_secret =" not in content


def test_project_delete_command_removes_project_and_children(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "remove me")
    db.create_task_log(task["id"], "codex", "build", "ok")
    session = db.create_session("demo", title="chat")
    db.create_session_message(session["id"], "user", "hello")

    result = CliRunner().invoke(main, ["project", "delete", "demo", "--yes"])

    assert result.exit_code == 0, result.output
    assert db.get_project("demo") is None
    assert db.list_tasks(project="demo") == []
    assert db.get_session(session["id"]) is None
    assert project_path.exists()


def test_webui_project_actions_create_and_delete_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "web-project"
    project_path.mkdir()

    created = webui_mod.create_project_action(str(project_path))

    assert created["ok"] is True
    assert created["created"] is True
    assert created["project"]["name"] == "web-project"
    assert db.get_project("web-project")["path"] == str(project_path.resolve())
    content = (project_path / "AGENTS.toml").read_text(encoding="utf-8")
    assert 'name = "web-project"' in content
    assert "app_secret =" not in content

    task = db.create_task("web-project", "remove me")
    deleted = webui_mod.delete_project_action("web-project")

    assert deleted["ok"] is True
    assert deleted["deleted_tasks"] == 1
    assert db.get_project("web-project") is None
    assert db.get_task(task["id"]) is None
    assert project_path.exists()


def test_resolve_project_for_prompt_syncs_defaults_from_config(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"
base_branch = "main"
default_mode = "codex"

[automation]
planner = "codex"
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = false
max_tasks = 4
max_retries = 2
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), default_mode="dual")
    monkeypatch.chdir(project_path)

    project = auto_cmd.resolve_project_for_prompt()

    assert project["name"] == "demo"
    assert project["default_mode"] == "codex"


def test_project_config_does_not_leak_from_workspace_when_project_has_no_agents_toml(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))

    auto_cfg = auto_cmd._project_config(db.get_project("demo"))
    run_cfg = run_cmd._project_config(db.get_project("demo"))

    assert auto_cfg is None
    assert run_cfg is None


def test_resolve_project_for_prompt_does_not_overwrite_parent_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "AGENTS.toml").write_text(
        """
[project]
name = "child-demo"
base_branch = "main"
default_mode = "codex"

[automation]
planner = "codex"
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = false
max_tasks = 4
max_retries = 2
""".strip(),
        encoding="utf-8",
    )

    db.register_project("parent-demo", str(parent), default_mode="dual")
    monkeypatch.chdir(child)

    project = auto_cmd.resolve_project_for_prompt()
    parent_project = db.get_project("parent-demo")

    assert project["name"] == "child-demo"
    assert project["path"] == str(child)
    assert parent_project["path"] == str(parent)


def test_get_current_project_task_stats_uses_current_directory(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    workdir = project_path / "src"
    workdir.mkdir(parents=True)

    db.register_project("demo", str(project_path))

    backlog = db.create_task("demo", "task backlog")
    in_progress = db.create_task("demo", "task running")
    done = db.create_task("demo", "task done")
    failed = db.create_task("demo", "task failed")
    cancelled = db.create_task("demo", "task cancelled")

    db.update_task(in_progress["id"], status="in_progress")
    db.update_task(done["id"], status="done")
    db.update_task(failed["id"], status="failed")
    db.update_task(cancelled["id"], status="cancelled")

    monkeypatch.chdir(workdir)

    stats = db.get_current_project_task_stats()

    assert stats == {
        "backlog": 1,
        "in_progress": 1,
        "done": 1,
        "failed": 1,
        "cancelled": 1,
        "total": 5,
    }
    assert db.get_current_project_stats() == stats


def test_get_current_project_task_stats_accepts_explicit_path(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    nested = project_path / "nested"
    nested.mkdir(parents=True)

    db.register_project("demo", str(project_path))
    db.create_task("demo", "task backlog")
    done = db.create_task("demo", "task done")
    db.update_task(done["id"], status="done")

    stats = db.get_current_project_task_stats(nested)

    assert stats == {
        "backlog": 1,
        "in_progress": 0,
        "done": 1,
        "failed": 0,
        "cancelled": 0,
        "total": 2,
    }
    assert db.get_task_stats_by_path(nested) == stats
    assert db.get_current_project_stats(nested) == stats


def test_get_current_project_task_stats_returns_none_when_project_is_unregistered(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert db.get_current_project_task_stats() is None
    assert db.get_current_project_stats() is None
    assert db.get_current_project_task_stats(outside) is None
    assert db.get_task_stats_by_path(outside) is None


def test_get_current_project_task_stats_prefers_deepest_registered_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)

    db.register_project("parent-demo", str(parent))
    db.register_project("child-demo", str(child))

    db.create_task("parent-demo", "parent backlog")
    parent_done = db.create_task("parent-demo", "parent done")
    db.update_task(parent_done["id"], status="done")

    child_running = db.create_task("child-demo", "child running")
    child_failed = db.create_task("child-demo", "child failed")
    db.update_task(child_running["id"], status="in_progress")
    db.update_task(child_failed["id"], status="failed")

    monkeypatch.chdir(child)

    stats = db.get_current_project_task_stats()

    assert stats == {
        "backlog": 0,
        "in_progress": 1,
        "done": 0,
        "failed": 1,
        "cancelled": 0,
        "total": 2,
    }

