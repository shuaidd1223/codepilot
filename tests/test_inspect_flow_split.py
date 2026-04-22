from __future__ import annotations

import json

from codepilot.commands import inspect as inspect_cmd


def test_collect_inspection_signals_collects_selected_only(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    called: list[str] = []

    monkeypatch.setattr(inspect_cmd, "collect_git_log", lambda *_: called.append("git_log") or "git-log")
    monkeypatch.setattr(inspect_cmd, "collect_failed_tasks", lambda *_: called.append("failed_tasks") or "failed")
    monkeypatch.setattr(inspect_cmd, "collect_todos", lambda *_: called.append("todos") or "todos")
    monkeypatch.setattr(inspect_cmd, "collect_ruff", lambda *_: called.append("ruff") or "ruff")
    monkeypatch.setattr(inspect_cmd, "collect_pytest_collect", lambda *_: called.append("pytest") or "pytest")
    monkeypatch.setattr(inspect_cmd, "collect_dependency_health", lambda *_: called.append("deps") or "deps")
    monkeypatch.setattr(inspect_cmd, "collect_code_metrics", lambda *_: called.append("code_metrics") or "metrics")

    signal_map = inspect_cmd.collect_inspection_signals(
        "demo",
        project,
        signals=("git_log", "deps", "complexity"),
    )

    assert signal_map["git_log"] == "git-log"
    assert signal_map["failed_tasks"] == "（跳过）"
    assert signal_map["deps"] == "deps"
    assert signal_map["code_metrics"] == "metrics"
    assert called == ["git_log", "deps", "code_metrics"]


def test_collect_inspection_signal_results_has_unified_model_and_order(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    called: list[str] = []

    monkeypatch.setattr(inspect_cmd, "collect_failed_tasks", lambda *_: called.append("failed_tasks") or "failed")
    monkeypatch.setattr(inspect_cmd, "collect_code_metrics", lambda *_: called.append("code_metrics") or "metrics")

    results = inspect_cmd.collect_inspection_signal_results(
        "demo",
        project,
        signals=("failed_tasks", "complexity"),
    )

    assert [item.key for item in results] == [
        "git_log",
        "failed_tasks",
        "todos",
        "ruff",
        "pytest",
        "deps",
        "code_metrics",
    ]
    assert [item.order for item in results] == [1, 2, 3, 4, 5, 6, 7]
    enabled = {item.key for item in results if item.enabled}
    assert enabled == {"failed_tasks", "code_metrics"}
    assert called == ["failed_tasks", "code_metrics"]
    skipped = [item.content for item in results if not item.enabled]
    assert skipped and all(content == "（跳过）" for content in skipped)


def test_build_inspection_prompt_renders_signal_sections_from_unified_model(monkeypatch):
    monkeypatch.setattr(inspect_cmd, "_existing_titles", lambda _project: "（无）")
    signal_results = [
        inspect_cmd.InspectSignalResult(
            key="git_log",
            title="最近 git 提交",
            order=1,
            enabled=True,
            content="git-log-body",
        ),
        inspect_cmd.InspectSignalResult(
            key="failed_tasks",
            title="最近失败或取消的任务",
            order=2,
            enabled=False,
            content="（跳过）",
        ),
    ]

    prompt = inspect_cmd._build_inspection_prompt(
        project_name="demo",
        max_new_tasks=2,
        signal_results=signal_results,
    )

    assert "## 信号 1：最近 git 提交\ngit-log-body" in prompt
    assert "## 信号 2：最近失败或取消的任务\n（跳过）" in prompt


def test_materialize_inspection_output_dry_run_skips_db_write(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    duplicate = {"title": "重复项", "goal": "同一个目标", "priority": "P1"}
    fresh = {"title": "新任务", "goal": "处理新的问题", "priority": "P2"}
    dup_key = inspect_cmd._dedup_key(duplicate["title"], duplicate["goal"])
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda *_: {dup_key})

    def _should_not_write(**kwargs):
        raise AssertionError("dry-run path should not write into db")

    monkeypatch.setattr(inspect_cmd.db, "create_task", _should_not_write)

    created, skipped = inspect_cmd._materialize_inspection_output(
        [duplicate, fresh],
        max_new_tasks=3,
        project_name="demo",
        project_path=project,
        priority="P3",
        agent="codex",
        dry_run=True,
    )

    assert created == [{"title": "新任务", "goal": "处理新的问题", "priority": "P2"}]
    assert skipped == [{"title": "重复项", "reason": "duplicate"}]


def test_emit_inspection_result_routes_json_and_terminal(monkeypatch):
    rendered: dict[str, object] = {}

    monkeypatch.setattr(
        inspect_cmd,
        "_print_result",
        lambda result, dry_run: rendered.update({"terminal_result": result, "terminal_dry_run": dry_run}),
    )
    monkeypatch.setattr(
        inspect_cmd.click,
        "echo",
        lambda payload: rendered.update({"json_payload": payload}),
    )

    terminal_result = {"project": "demo", "created": [], "skipped": [], "candidates_total": 0}
    inspect_cmd._emit_inspection_result(terminal_result, dry_run=True, json_mode=False)
    assert rendered["terminal_result"] == terminal_result
    assert rendered["terminal_dry_run"] is True

    json_result = {"project": "demo", "created": [{"id": 1}], "skipped": [], "candidates_total": 1}
    inspect_cmd._emit_inspection_result(json_result, dry_run=False, json_mode=True)
    payload = json.loads(rendered["json_payload"])
    assert payload["ok"] is True
    assert payload["command"] == "inspect"
    assert payload["data"]["candidates_total"] == 1


def test_emit_inspection_result_maps_error_into_contract(monkeypatch):
    rendered: dict[str, object] = {}

    monkeypatch.setattr(
        inspect_cmd.click,
        "echo",
        lambda payload: rendered.update({"json_payload": payload}),
    )

    inspect_cmd._emit_inspection_result(
        {"project": "demo", "created": [], "skipped": [], "candidates_total": 0, "error": "llm timeout"},
        dry_run=False,
        json_mode=True,
    )

    payload = json.loads(rendered["json_payload"])
    assert payload["ok"] is False
    assert payload["command"] == "inspect"
    assert payload["data"]["project"] == "demo"
    assert payload["error"]["code"] == "inspect_failed"
