from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.ai_support.agent_support import command_manifest
from codepilot.ai_support.agent_task_template import task_template_schema
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands.plan import write_plan_artifact
from codepilot.core.task_template import (
    missing_task_template_sections,
    unreplaced_task_template_placeholders,
)
from codepilot.core.workflow_state import get_agent_session, read_workflow_state
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _register_demo(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("explore and doctor commands live here\n", encoding="utf-8")
    (project / "codepilot").mkdir()
    (project / "codepilot" / "commands").mkdir(parents=True, exist_ok=True)
    (project / "codepilot" / "commands" / "explore.py").write_text("# explore\n", encoding="utf-8")
    (project / "codepilot" / "commands" / "doctor.py").write_text("# doctor\n", encoding="utf-8")
    return db.register_project("demo", str(project))


def test_plan_json_writes_artifact_without_creating_tasks(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    before = db.get_task_stats("demo")["total"]

    result = CliRunner().invoke(main, ["plan", "-p", "demo", "新增 explore", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["source"] == "text"
    assert data["task_candidates"]
    assert data["risks"]
    assert data["verification_plan"]
    plan_path = Path(data["plan_path"])
    assert plan_path.exists()
    assert plan_path.is_relative_to(Path(project["path"]))
    assert db.get_task_stats("demo")["total"] == before

    text = plan_path.read_text(encoding="utf-8")
    for heading in ("## 执行顺序", "## 文件范围", "## 风险", "## 验证矩阵", "## 任务候选", "## 后续选择"):
        assert heading in text


def test_plan_text_input_json_contract_records_artifacts_without_backlog(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    before = db.get_task_stats("demo")["total"]
    requirement = "根据 CodePilot 巡检上下文推进：清理命令模块残留的 ruff 告警。先生成可审查计划，不导入 backlog。"

    result = CliRunner().invoke(main, ["plan", "-p", "demo", requirement, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "plan"
    data = payload["data"]
    assert data["source"] == "text"
    assert data["source_path"] is None

    project_path = Path(project["path"])
    plan_path = Path(data["plan_path"])
    context_path = Path(data["context_path"])
    task_batch_path = Path(data["task_batch_path"])
    for artifact_path in (plan_path, context_path, task_batch_path):
        assert artifact_path.exists()
        assert artifact_path.is_relative_to(project_path)

    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["requirement"] == requirement
    assert context["source"] == "text"
    assert context["source_path"] is None
    assert context["task_batch_path"] == str(task_batch_path)
    assert context["task_candidates"] == data["task_candidates"]
    assert context["verification_plan"] == data["verification_plan"]
    assert context["next_actions"] == data["next_actions"]
    assert all({"criterion", "command", "expected"}.issubset(item) for item in context["verification_plan"])

    task_batch = json.loads(task_batch_path.read_text(encoding="utf-8"))
    assert len(task_batch) == len(data["task_candidates"])
    assert all({"agent", "content", "priority", "title"}.issubset(item) for item in task_batch)

    import_action = next(action for action in data["next_actions"] if action["id"] == "import_tasks")
    assert str(task_batch_path) in import_action["suggested_command"]
    assert str(context_path) not in import_action["suggested_command"]
    assert db.get_task_stats("demo")["total"] == before


def test_plan_from_spec_consumes_clarify_spec(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    spec_dir = Path(project["path"]) / ".codepilot" / "specs"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "example.md"
    spec_path.write_text(
        "# Clarify Spec: 改进 doctor\n\n"
        "## 目标\n- 增强 doctor 的服务检查。\n\n"
        "## 验收标准\n- JSON 输出包含服务状态。\n",
        encoding="utf-8",
    )
    before = db.get_task_stats("demo")["total"]

    result = CliRunner().invoke(main, ["plan", "-p", "demo", "--from-spec", str(spec_path), "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["source"] == "spec"
    assert data["source_path"] == str(spec_path)
    assert "doctor" in data["summary"].lower()
    assert data["task_candidates"]
    assert db.get_task_stats("demo")["total"] == before


def test_plan_from_inspect_context_uses_preview_candidates(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    context_dir = Path(project["path"]) / ".codepilot" / "context"
    context_dir.mkdir(parents=True)
    context_path = context_dir / "inspect-context.json"
    context_path.write_text(
        json.dumps(
            {
                "artifact_type": "inspect",
                "summary": "清理命令模块残留的 ruff 告警",
                "created_preview": [
                    {
                        "title": "清理命令模块残留的 ruff 告警",
                        "goal": "移除命令模块中已报告的未使用导入和无占位符 f-string。",
                        "priority": "P3",
                        "files": [
                            "codepilot/commands/auto_workflow.py",
                            "codepilot/commands/cleanup.py",
                        ],
                        "acceptance_criteria": [
                            "signal 中列出的 F401 未使用导入已删除或改为实际使用",
                            "signal 中列出的 F541 已消除",
                        ],
                        "verification_commands": [
                            "ruff check codepilot/commands/auto_workflow.py codepilot/commands/cleanup.py",
                            "pytest -n auto --dist loadfile -m \"not slow\" tests/test_inspect_flow_split.py -q",
                        ],
                        "evidence": "signal 4: codepilot/commands/auto_workflow.py F401; cleanup.py F541",
                    }
                ],
                "report_only": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    before = db.get_task_stats("demo")["total"]

    result = write_plan_artifact(
        project,
        "根据 CodePilot 巡检上下文推进：清理命令模块残留的 ruff 告警。先生成可审查计划，不导入 backlog。",
        source="inspect",
        source_path=str(context_path),
        use_wiki=False,
    )

    assert result["source"] == "inspect"
    assert result["source_path"] == str(context_path)
    assert result["summary"] == "清理命令模块残留的 ruff 告警"
    assert result["files"] == [
        "codepilot/commands/auto_workflow.py",
        "codepilot/commands/cleanup.py",
    ]
    assert result["task_candidates"][0]["title"] == "清理命令模块残留的 ruff 告警"
    assert result["task_candidates"][0]["priority"] == "P3"
    assert result["task_candidates"][0]["acceptance_criteria"] == [
        "signal 中列出的 F401 未使用导入已删除或改为实际使用",
        "signal 中列出的 F541 已消除",
    ]
    assert [item["command"] for item in result["verification_plan"]] == [
        "ruff check codepilot/commands/auto_workflow.py codepilot/commands/cleanup.py",
        "pytest -n auto --dist loadfile -m \"not slow\" tests/test_inspect_flow_split.py -q",
    ]
    assert db.get_task_stats("demo")["total"] == before

    plan_text = Path(result["plan_path"]).read_text(encoding="utf-8")
    assert "codepilot/commands/auto_workflow.py" in plan_text
    assert "signal 4: codepilot/commands/auto_workflow.py F401; cleanup.py F541" in plan_text


def test_plan_marks_workflow_state_complete(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["plan", "-p", "demo", "优化 doctor", "--json"])

    assert result.exit_code == 0, result.output
    state = read_workflow_state(project["path"], mode="plan")
    assert state["active"] is False
    assert state["current_phase"] == "completed"
    assert state["artifact_paths"]["plan"] == json.loads(result.output)["data"]["plan_path"]


def test_plan_from_spec_advances_existing_agent_session(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    clarify = CliRunner().invoke(main, ["clarify", "-p", "demo", "改进 doctor", "--json"])
    assert clarify.exit_code == 0, clarify.output
    clarify_data = json.loads(clarify.output)["data"]
    before = get_agent_session(project["path"])
    assert before is not None

    result = CliRunner().invoke(main, ["plan", "-p", "demo", "--from-spec", clarify_data["artifact_path"], "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    session = get_agent_session(project["path"])
    assert session is not None
    assert session["session_id"] == before["session_id"]
    assert session["current_phase"] == "plan"
    assert session["artifact_paths"]["spec"] == clarify_data["artifact_path"]
    assert session["artifact_paths"]["plan"] == data["plan_path"]
    assert session["artifact_paths"]["context"] == data["context_path"]
    assert session["artifact_paths"]["task_batch"] == data["task_batch_path"]
    assert session["next_actions"] == [action["id"] for action in data["next_actions"]]
    assert session["next_action_details"] == data["next_actions"]
    assert [item["phase"] for item in session["phase_history"]][-2:] == ["clarify", "plan"]


def test_plan_json_includes_next_actions(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["plan", "-p", "demo", "新增 explore", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert "next_actions" in data
    assert isinstance(data["next_actions"], list)
    assert len(data["next_actions"]) > 0
    for action in data["next_actions"]:
        assert "id" in action
        assert "label" in action
        assert "risk" in action
        assert "suggested_command" in action


def test_plan_json_writes_importable_task_batch_artifact(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["plan", "-p", "demo", "新增 explore", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    task_batch_path = Path(data["task_batch_path"])
    context_path = Path(data["context_path"])
    assert task_batch_path.exists()
    assert task_batch_path.is_relative_to(Path(project["path"]))
    assert task_batch_path != context_path

    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["task_batch_path"] == str(task_batch_path)

    items = json.loads(task_batch_path.read_text(encoding="utf-8"))
    assert isinstance(items, list)
    assert len(items) == len(data["task_candidates"])
    schema = task_template_schema(language="zh-CN")
    placeholder_names = [item["name"] for item in schema["placeholders"]]
    for item in items:
        assert {"agent", "content", "priority", "title"}.issubset(item)
        assert item["title"]
        assert item["priority"] in {"P0", "P1", "P2", "P3"}
        assert item["agent"]
        assert missing_task_template_sections(item["content"]) == []
        assert unreplaced_task_template_placeholders(
            item["content"],
            placeholder_names=placeholder_names,
        ) == []

    import_action = next(action for action in data["next_actions"] if action["id"] == "import_tasks")
    command = import_action["suggested_command"]
    assert str(task_batch_path) in command
    assert str(context_path) not in command
    assert "--json" not in command

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))
    import_result = CliRunner().invoke(main, ["add", "-p", "demo", "-f", str(task_batch_path)])
    assert import_result.exit_code == 0, import_result.output
    imported = db.list_tasks(project="demo")
    assert len(imported) == len(items)
    assert [task["title"] for task in imported] == [item["title"] for item in items]


def test_ai_manifest_includes_plan_command():
    manifest = command_manifest(command_name="codepilot")

    assert any(cmd["name"] == "plan" for cmd in manifest["commands"])


def test_plan_no_backlog_boundary_is_documented_for_agents():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "AI_MANIFEST.json").read_text(encoding="utf-8"))
    doc = (root / "docs" / "AI与Agent调用手册.zh-CN.md").read_text(encoding="utf-8")
    plan_command = next(cmd for cmd in manifest["commands"] if cmd["name"] == "plan")
    plan_output = next(
        item
        for item in manifest["structured_outputs"]
        if item["command"] == "codepilot plan -p <project-name> <requirement> --json"
    )

    assert "reviewable plan artifact" in plan_output["purpose"]
    assert "should not enter backlog" in plan_command["when_to_use"]
    assert "`plan` 的文本输入会生成可审查计划 artifact" in doc
    assert "默认只写入 `.codepilot/plans/plan-*.md`" in doc
    assert "不会导入 backlog、不会启动执行器" in doc
    assert "`task_batch_path` 指向可导入的 tasks JSON" in doc
    assert "显式调用 `codepilot workflow next -p <项目名> --action import_tasks --json`" in doc
