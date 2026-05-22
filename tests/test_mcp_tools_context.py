from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.commands.note import read_notepad
from codepilot.mcp.server import MCPProjectContext, create_mcp_server
from codepilot.mcp.tool_registry import default_registry
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _init_demo_project(tmp_path: Path, monkeypatch) -> Path:
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def _context_server(project_path: Path):
    import codepilot.mcp.tools.context  # noqa: F401

    return create_mcp_server(
        MCPProjectContext(project_path=project_path, project="demo"),
        registry=default_registry,
        include_health=False,
        bind_sdk=False,
    )


def _tool_by_name(name: str) -> dict[str, Any]:
    import codepilot.mcp.tools.context  # noqa: F401

    return default_registry.get(name).schema


def _error_code(payload: dict[str, Any]) -> str:
    return payload["structuredContent"]["error"]["code"]


def test_context_tools_register_independent_contracts():
    import codepilot.mcp.tools.context  # noqa: F401

    expected = {
        "wiki_query",
        "wiki_add",
        "note_add",
        "explore",
        "inspect_project",
        "inspect_workflow",
        "workflow_status",
        "workflow_next",
        "hook_trigger",
    }

    names = {tool.name for tool in default_registry.list()}

    assert expected <= names
    for name in expected:
        schema = _tool_by_name(name)
        assert schema["description"]
        assert schema["inputSchema"]["type"] == "object"
        assert schema["inputSchema"]["additionalProperties"] is False
        assert callable(default_registry.get(name).func)


def test_context_tool_input_schemas_capture_required_fields():
    assert _tool_by_name("wiki_query")["inputSchema"]["required"] == ["project", "query"]
    assert _tool_by_name("wiki_add")["inputSchema"]["required"] == ["project", "title", "body"]
    assert _tool_by_name("note_add")["inputSchema"]["required"] == ["project", "content"]
    assert _tool_by_name("explore")["inputSchema"]["required"] == ["project", "query"]
    assert _tool_by_name("inspect_project")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("inspect_workflow")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("workflow_status")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("workflow_next")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("hook_trigger")["inputSchema"]["required"] == ["project"]

    wiki_props = _tool_by_name("wiki_add")["inputSchema"]["properties"]
    assert wiki_props["project"] == {"type": "string"}
    assert wiki_props["tags"] == {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ],
        "default": None,
    }


def test_wiki_and_note_context_tools_use_existing_project_memory(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    added = server.call_tool(
        "wiki_add",
        {
            "project": "demo",
            "title": "构建命令",
            "body": "运行 pytest tests/test_mcp_tools_context.py -q",
            "tags": ["test", "mcp"],
        },
    )
    queried = server.call_tool("wiki_query", {"project": "demo", "query": "构建", "limit": 5})
    note = server.call_tool(
        "note_add",
        {"project": "demo", "content": "上下文 MCP 工具复用 note 接口", "section": "priority"},
    )

    assert added["project"] == "demo"
    assert added["page"]["title"] == "构建命令"
    assert queried["project"] == "demo"
    assert queried["results"][0]["title"] == "构建命令"
    assert note["section"] == "priority"
    notepad = read_notepad(db.get_project("demo"))
    assert notepad["sections"]["priority"][0]["content"] == "上下文 MCP 工具复用 note 接口"


def test_explore_context_tool_preserves_readonly_rejection(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    result = server.call_tool("explore", {"project": "demo", "query": "delete a file"})

    assert result["project"]["name"] == "demo"
    assert result["status"] == "rejected"
    assert result["rejected"] is True
    assert result["evidence"] == []
    assert "只读" in result["limitations"][0]


def test_inspect_project_returns_serializable_signal_results(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    from codepilot.commands import inspect as inspect_cmd

    captured: dict[str, Any] = {}

    def _fake_collect(project_name, path, *, signals):
        captured["project_name"] = project_name
        captured["path"] = path
        captured["signals"] = signals
        return {
            "git_log": "git-log",
            "failed_tasks": "failed-tasks",
            "todos": "（跳过）",
        }

    monkeypatch.setattr(inspect_cmd, "collect_inspection_signals", _fake_collect)

    result = server.call_tool(
        "inspect_project",
        {"project": "demo", "signals": ["git_log", "failed_tasks"]},
    )

    assert result["project"] == "demo"
    assert result["project_path"] == str(project_path.resolve())
    assert captured == {
        "project_name": "demo",
        "path": project_path,
        "signals": ("git_log", "failed_tasks"),
    }
    assert result["signal_summary"] == {
        "requested": ["git_log", "failed_tasks"],
        "returned": 3,
        "enabled": ["git_log", "failed_tasks"],
        "errors": [],
    }
    assert result["errors"] == []
    enabled = {item["key"]: item for item in result["signals"] if item["enabled"]}
    assert enabled["git_log"]["content"] == "git-log"
    assert enabled["failed_tasks"]["content"] == "failed-tasks"
    assert all(isinstance(item, dict) for item in result["signals"])


def test_inspect_project_returns_structured_error_on_inspect_failure(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    from codepilot.commands import inspect as inspect_cmd

    def _fail_collect(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(inspect_cmd, "collect_inspection_signals", _fail_collect)

    result = server.call_tool(
        "inspect_project",
        {"project": "demo", "signals": ["git_log"]},
    )

    assert _error_code(result) == "inspect_error"
    assert result["structuredContent"]["error"]["details"] == {
        "project": "demo",
        "project_path": str(project_path.resolve()),
        "signals": ["git_log"],
    }


def test_workflow_context_tools_expose_status_next_and_inspect_run(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)
    (project_path / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")

    from codepilot.commands import inspect as inspect_cmd

    monkeypatch.setattr(
        inspect_cmd,
        "collect_inspection_signal_results",
        lambda *_args, **_kwargs: [
            inspect_cmd.InspectSignalResult(
                key="todos",
                title="代码里的 TODO/FIXME/XXX",
                order=3,
                enabled=True,
                content="foo.py:1: TODO handle timeout",
            )
        ],
    )
    monkeypatch.setattr(
        inspect_cmd,
        "load_project_config",
        lambda *_args, **_kwargs: type(
            "Cfg",
            (),
            {
                "inspect": type(
                    "Inspect",
                    (),
                    {
                        "max_new_tasks_per_round": 3,
                        "signals": ("todos",),
                        "priority": "P3",
                        "auto_execute": False,
                    },
                )(),
                "automation": type("Automation", (), {"agent_language": "zh-CN"})(),
            },
        )(),
    )
    monkeypatch.setattr(inspect_cmd, "resolve_planner", lambda _cfg, _kind, explicit=None: explicit or "codex")
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda _project_name: set())
    monkeypatch.setattr(inspect_cmd.db, "list_tasks", lambda **_kwargs: [])
    monkeypatch.setattr(
        inspect_cmd,
        "_call_llm",
        lambda *_args, **_kwargs: {
            "candidates": [
                {
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py:1 的 TODO。",
                    "priority": "P3",
                    "kind": "bug",
                    "evidence": "signal 3: foo.py:1 TODO handle timeout",
                    "files": ["foo.py"],
                    "acceptance_criteria": ["TODO 已处理。"],
                    "verification_commands": ["git diff --check"],
                    "effort": "small",
                }
            ]
        },
    )

    inspected = server.call_tool("inspect_workflow", {"project": "demo"})
    status = server.call_tool("workflow_status", {"project": "demo"})
    next_actions = server.call_tool("workflow_next", {"project": "demo"})

    assert inspected["ok"] is True
    assert inspected["workflow_context"]["context_path"]
    assert status["agent_session"]["current_phase"] == "explore"
    assert any(action["id"] == "create_inspect_tasks" for action in next_actions["next_actions"])


def test_workflow_next_tool_auto_uses_shared_policy(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)
    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    write_inspect_workflow_context(
        db.get_project("demo"),
        {
            "project": "demo",
            "created": [
                {
                    "candidate_id": "inspect-actionable",
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py 中的超时 TODO。",
                    "priority": "P2",
                    "reason": "todo_signal",
                    "files": ["foo.py"],
                    "evidence": "signal 1: foo.py",
                }
            ],
            "report_only": [],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 1, "report_only_count": 0},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-mcp-auto",
    )

    payload = server.call_tool("workflow_next", {"project": "demo", "auto": True})

    assert payload["auto"] is True
    assert payload["selected_reason"] == "low_risk_inspect_plan"
    assert payload["action"]["id"] == "plan_from_inspect"


def test_workflow_next_tool_returns_structured_error_for_missing_project(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    result = server.call_tool("workflow_status", {"project": "missing"})

    assert _error_code(result) == "project_not_found"


def test_hook_trigger_uses_existing_project_hook_path(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)
    captured: dict[str, Any] = {}

    def _fake_dispatch(project_root, event):
        captured["project_root"] = str(project_root)
        captured["event"] = event
        return [{"name": "audit", "status": "delivered"}]

    monkeypatch.setattr("codepilot.core.event_plugins.dispatch_event_to_sinks", _fake_dispatch)

    result = server.call_tool(
        "hook_trigger",
        {
            "project": "demo",
            "provider": "codex",
            "event_type": "agent.prompt.submitted",
        },
    )

    assert result["project"] == "demo"
    assert result["logged"] is True
    assert result["delivered"] == 1
    assert captured["project_root"] == str(project_path)
    assert captured["event"]["payload"]["provider"] == "codex"
    assert (project_path / ".codepilot" / "hooks" / "logs" / "hook-events.jsonl").is_file()


def test_context_tools_return_structured_errors(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    missing = server.call_tool("wiki_add", {"project": "demo", "title": "x"})
    wrong_type = server.call_tool("wiki_query", {"project": "demo", "query": "x", "limit": "bad"})
    missing_project = server.call_tool("note_add", {"project": "missing", "content": "x"})
    wiki_business = server.call_tool(
        "wiki_add",
        {"project": "demo", "title": "密钥", "body": "FEISHU_APP_SECRET=abc"},
    )
    note_business = server.call_tool(
        "note_add",
        {"project": "demo", "content": "x", "section": "unknown"},
    )
    hook_business = server.call_tool(
        "hook_trigger",
        {"project": "demo", "provider": "bad-provider"},
    )
    inspect_missing_arg = server.call_tool("inspect_project", {})
    inspect_missing_project = server.call_tool(
        "inspect_project",
        {"project": "missing"},
    )
    db.register_project("broken", str(tmp_path / "missing-path"))
    inspect_bad_path = server.call_tool(
        "inspect_project",
        {"project": "broken"},
    )

    assert _error_code(missing) == "invalid_arguments"
    assert _error_code(wrong_type) == "invalid_arguments"
    assert _error_code(missing_project) == "project_not_found"
    assert _error_code(wiki_business) == "wiki_error"
    assert _error_code(note_business) == "note_error"
    assert _error_code(hook_business) == "hook_error"
    assert _error_code(inspect_missing_arg) == "invalid_arguments"
    assert _error_code(inspect_missing_project) == "project_not_found"
    assert _error_code(inspect_bad_path) == "project_path_not_found"
