from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from codepilot.cli import main
from codepilot.core.task_template import missing_task_template_sections
from codepilot.commands import inspect as inspect_cmd
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


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


def test_build_inspection_prompt_defaults_to_english_output_language(monkeypatch):
    monkeypatch.setattr(inspect_cmd, "_existing_titles", lambda _project: "(none)")

    prompt = inspect_cmd._build_inspection_prompt(
        project_name="demo",
        max_new_tasks=2,
        signal_results=[],
    )

    assert "The following output fields MUST be English" in prompt
    assert "The following output fields MUST be Chinese" not in prompt


def test_build_inspection_prompt_can_request_chinese_output_language(monkeypatch):
    monkeypatch.setattr(inspect_cmd, "_existing_titles", lambda _project: "（无）")

    prompt = inspect_cmd._build_inspection_prompt(
        project_name="demo",
        max_new_tasks=2,
        signal_results=[],
        language="zh-CN",
    )

    assert "The following output fields MUST be Chinese" in prompt


def test_materialize_inspection_output_dry_run_skips_db_write(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")
    duplicate = {"title": "重复项", "goal": "同一个目标", "priority": "P1"}
    fresh = {
        "title": "新任务",
        "goal": "处理 foo.py 的新问题",
        "priority": "P2",
        "evidence": "signal 3: foo.py:1 TODO handle timeout",
    }
    dup_key = inspect_cmd._dedup_key(duplicate["title"], duplicate["goal"])
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda *_: {dup_key})
    monkeypatch.setattr(inspect_cmd.db, "list_tasks", lambda **kwargs: [])

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

    assert created == [{"title": "新任务", "goal": "处理 foo.py 的新问题", "priority": "P2", "files": ["foo.py"]}]
    assert skipped == [{"title": "重复项", "reason": "duplicate"}]


def test_materialize_inspection_output_skips_repeated_inspector_failed_title(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda *_: set())
    monkeypatch.setattr(
        inspect_cmd.db,
        "list_tasks",
        lambda **kwargs: [
            {
                "title": "重复巡检任务",
                "status": "failed",
                "source": "inspector",
            }
        ],
    )

    def _should_not_write(**kwargs):
        raise AssertionError("repeated inspector title should be skipped before db write")

    monkeypatch.setattr(inspect_cmd.db, "create_task", _should_not_write)

    created, skipped = inspect_cmd._materialize_inspection_output(
        [{"title": "重复巡检任务", "goal": "继续做同一件事", "priority": "P2"}],
        max_new_tasks=1,
        project_name="demo",
        project_path=project,
        priority="P3",
        agent="codex",
        dry_run=False,
    )

    assert created == []
    assert skipped == [{"title": "重复巡检任务", "reason": "duplicate_title_history"}]


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


def test_print_result_includes_report_only_count_and_reasons(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(inspect_cmd, "echo", lines.append)

    inspect_cmd._print_result(
        {
            "project": "demo",
            "candidates_total": 1,
            "created": [],
            "skipped": [],
            "dropped": [],
            "report_only": [
                {
                    "title": "报告 app.py 复杂度",
                    "priority": "P3",
                    "files": ["app.py"],
                    "reason": "code_metrics_only_weak_signal",
                }
            ],
        },
        dry_run=True,
    )

    rendered = "\n".join(lines)
    assert "仅报告 1" in rendered
    assert "报告 app.py 复杂度" in rendered
    assert "code_metrics_only_weak_signal" in rendered


def test_has_substantive_signal_detects_bracketed_markers():
    empty_results = [
        inspect_cmd.InspectSignalResult(key="git_log", title="git", order=1, enabled=False, content="（跳过）"),
        inspect_cmd.InspectSignalResult(key="todos", title="todos", order=2, enabled=True, content="（无）"),
        inspect_cmd.InspectSignalResult(key="ruff", title="ruff", order=3, enabled=True, content="（ruff 无发现）"),
        inspect_cmd.InspectSignalResult(key="deps", title="deps", order=4, enabled=True, content="  "),
    ]
    assert inspect_cmd._has_substantive_signal(empty_results) is False

    mixed_results = [
        *empty_results,
        inspect_cmd.InspectSignalResult(
            key="failed_tasks",
            title="失败任务",
            order=5,
            enabled=True,
            content="#12 [failed] 解析错误\n#13 [cancelled] 超时",
        ),
    ]
    assert inspect_cmd._has_substantive_signal(mixed_results) is True


def test_run_inspection_short_circuits_when_all_signals_empty(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()

    def _only_empty_signals(*_, **__):
        return [
            inspect_cmd.InspectSignalResult(key="git_log", title="git", order=1, enabled=True, content="（近 7 天无提交）"),
            inspect_cmd.InspectSignalResult(key="todos", title="todos", order=2, enabled=True, content="（无）"),
        ]

    monkeypatch.setattr(inspect_cmd, "collect_inspection_signal_results", _only_empty_signals)

    def _llm_should_not_be_called(*_, **__):
        raise AssertionError("LLM must not be called when every signal is empty")

    monkeypatch.setattr(inspect_cmd, "_call_llm", _llm_should_not_be_called)
    monkeypatch.setattr(inspect_cmd, "load_project_config", lambda *_: None)

    result = inspect_cmd.run_inspection(
        {"name": "demo", "path": str(project)},
        signals=("git_log", "todos"),
        dry_run=True,
    )

    assert result["candidates_total"] == 0
    assert result["created"] == []
    assert result["reason"] == "no_substantive_signals"
    assert "硬规划" in result["note"]


def test_run_inspection_skips_classifier_provider_by_default(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        inspect_cmd,
        "collect_inspection_signal_results",
        lambda *_args, **_kwargs: [
            inspect_cmd.InspectSignalResult(
                key="todos",
                title="代码里的 TODO/FIXME/XXX",
                order=1,
                enabled=True,
                content="codepilot/foo.py:42: TODO handle timeout",
            )
        ],
    )
    monkeypatch.setattr(
        inspect_cmd,
        "load_project_config",
        lambda *_args: type(
            "Cfg",
            (),
            {
                "classifier": type(
                    "Classifier",
                    (),
                    {"enabled": True, "provider": "openai-gpt4o", "model": "gpt-test"},
                )(),
                "providers": {},
                "get_provider_api_key": lambda self, _provider: "sk-test",
            },
        )(),
    )

    def _capture_llm(_prompt, classifier_provider, classifier_model, *_args, **_kwargs):
        captured["classifier_provider"] = classifier_provider
        captured["classifier_model"] = classifier_model
        return {"candidates": []}

    monkeypatch.setattr(inspect_cmd, "_call_llm", _capture_llm)

    result = inspect_cmd.run_inspection({"name": "demo", "path": str(project)}, dry_run=True)

    assert result["candidates_total"] == 0
    assert captured == {"classifier_provider": "", "classifier_model": ""}


@pytest.mark.parametrize("dry_run", [True, False])
def test_run_inspection_reports_weak_candidates_without_materializing_and_keeps_strong(tmp_path, monkeypatch, dry_run):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")
    (project / "app.py").write_text(
        "import os\n\n\ndef tangled(a, b):\n    if a:\n        return b\n    return a\n",
        encoding="utf-8",
    )
    created_titles: list[str] = []

    signal_results = [
        inspect_cmd.InspectSignalResult(
            key="git_log",
            title="最近 git 提交",
            order=1,
            enabled=True,
            content="abc123 Update app.py",
        ),
        inspect_cmd.InspectSignalResult(
            key="todos",
            title="代码里的 TODO/FIXME/XXX",
            order=3,
            enabled=True,
            content="foo.py:1: TODO handle timeout",
        ),
        inspect_cmd.InspectSignalResult(
            key="ruff",
            title="ruff lint 报告",
            order=4,
            enabled=True,
            content="app.py:1:8: F401 `os` imported but unused",
        ),
        inspect_cmd.InspectSignalResult(
            key="code_metrics",
            title="代码规模与复杂度线索",
            order=7,
            enabled=True,
            content="- app.py:4 tangled 分支复杂度约 12",
        ),
    ]
    monkeypatch.setattr(inspect_cmd, "collect_inspection_signal_results", lambda *_args, **_kwargs: signal_results)
    monkeypatch.setattr(
        inspect_cmd,
        "load_project_config",
        lambda *_args, **_kwargs: SimpleNamespace(automation=SimpleNamespace(agent_language="zh-CN")),
    )
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda _project_name: set())
    monkeypatch.setattr(inspect_cmd.db, "list_tasks", lambda **_kwargs: [])

    def fake_create_task(**kwargs):
        created_titles.append(str(kwargs["title"]))
        return {"id": len(created_titles), **kwargs}

    monkeypatch.setattr(inspect_cmd.db, "create_task", fake_create_task)

    def fake_call_llm(*_args, **_kwargs):
        return {
            "candidates": [
                {
                    "title": "记录 P4 TODO 线索",
                    "goal": "记录 foo.py:1 的 TODO，后续人工判断是否需要整理。",
                    "priority": "P4",
                    "rationale": "TODO 只有低优先级整理价值。",
                    "kind": "chore",
                    "evidence": "signal 3: foo.py:1 TODO handle timeout",
                    "files": ["foo.py"],
                    "acceptance_criteria": ["foo.py:1 的 TODO 已被人工复核。"],
                    "verification_commands": ["git diff --check"],
                    "effort": "small",
                },
                {
                    "title": "报告 app.py 复杂度",
                    "goal": "报告 app.py:4 的复杂度热点，先不自动创建重构任务。",
                    "priority": "P3",
                    "rationale": "复杂度信号单独出现，缺少失败或 lint 佐证。",
                    "kind": "refactor",
                    "evidence": "signal 7: app.py:4 tangled 分支复杂度约 12",
                    "files": ["app.py"],
                    "acceptance_criteria": ["app.py:4 的复杂度热点已被人工评估。"],
                    "verification_commands": ["pytest -n auto --dist loadfile -m \"not slow\" -q"],
                    "effort": "small",
                },
                {
                    "title": "报告 app.py 提交线索",
                    "goal": "报告 abc123 对 app.py 的普通提交，等待人工确认是否需要后续动作。",
                    "priority": "P3",
                    "rationale": "最近提交只显示普通更新，没有异常词。",
                    "kind": "chore",
                    "evidence": "signal 1: abc123 Update app.py",
                    "files": ["app.py"],
                    "acceptance_criteria": ["abc123 的 app.py 提交已被人工评估。"],
                    "verification_commands": ["git diff --check"],
                    "effort": "small",
                },
                {
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py:1 的 TODO，避免超时路径继续缺实现。",
                    "priority": "P3",
                    "rationale": "TODO 指向明确文件和处理目标。",
                    "kind": "bug",
                    "evidence": "signal 3: foo.py:1 TODO handle timeout",
                    "files": ["foo.py"],
                    "acceptance_criteria": ["foo.py:1 的超时 TODO 已处理。"],
                    "verification_commands": ["pytest -n auto --dist loadfile -m \"not slow\" -q"],
                    "effort": "small",
                },
                {
                    "title": "修复 app.py F401",
                    "goal": "移除 app.py:1 的未使用导入，保持 lint 报告干净。",
                    "priority": "P3",
                    "rationale": "ruff 明确报告 F401。",
                    "kind": "chore",
                    "evidence": "signal 4: app.py:1:8: F401 `os` imported but unused",
                    "files": ["app.py"],
                    "acceptance_criteria": ["app.py:1 的 F401 已消除。"],
                    "verification_commands": ["ruff check app.py"],
                    "effort": "small",
                },
            ]
        }

    monkeypatch.setattr(inspect_cmd, "_call_llm", fake_call_llm)

    result = inspect_cmd.run_inspection(
        {"name": "demo", "path": str(project)},
        signals=("git_log", "todos", "ruff", "complexity"),
        max_new_tasks=5,
        dry_run=dry_run,
    )

    assert [item["title"] for item in result["created"]] == ["修复 foo.py 超时 TODO", "修复 app.py F401"]
    assert created_titles == ([] if dry_run else ["修复 foo.py 超时 TODO", "修复 app.py F401"])
    assert result["report_only_count"] == 3
    assert {item["title"]: item["reason"] for item in result["report_only"]} == {
        "记录 P4 TODO 线索": "priority_p4_report_only",
        "报告 app.py 复杂度": "code_metrics_only_weak_signal",
        "报告 app.py 提交线索": "git_log_only_benign",
    }
    assert all(item["files"] for item in result["report_only"])


def test_call_llm_skips_unavailable_api_and_marks_fallback(tmp_path, monkeypatch):
    calls: dict[str, object] = {"api": 0, "marks": []}

    def _api_unavailable(*_args, **_kwargs):
        calls["api"] = int(calls["api"]) + 1
        raise RuntimeError("invalid api key")

    def _local_cli(*_args, **_kwargs):
        return {"candidates": []}

    def _mark(provider_key, provider, reason, **kwargs):
        calls["marks"].append(
            {
                "provider_key": provider_key,
                "provider": provider.name,
                "reason": reason,
                "source": kwargs.get("source"),
                "project_path": kwargs.get("project_path"),
            }
        )

    monkeypatch.setattr(inspect_cmd, "_run_api_provider", _api_unavailable)
    monkeypatch.setattr(inspect_cmd, "_run_claude_schema_prompt", _local_cli)
    monkeypatch.setattr(inspect_cmd, "mark_provider_unavailable", _mark)

    payload = inspect_cmd._call_llm(
        "prompt",
        "openai-gpt4o",
        "",
        "sk-invalid",
        None,
        str(tmp_path),
        5,
        planner="claude",
    )

    assert payload == {"candidates": []}
    assert calls["api"] == 1
    assert calls["marks"] == [
        {
            "provider_key": "openai-gpt4o",
            "provider": "OpenAI GPT-4o",
            "reason": "invalid api key",
            "source": "inspect",
            "project_path": str(tmp_path),
        }
    ]


def test_filter_candidates_drops_generic_overlong_and_ungrounded():
    signal_results = [
        inspect_cmd.InspectSignalResult(
            key="todos",
            title="代码里的 TODO/FIXME/XXX",
            order=3,
            enabled=True,
            content="codepilot/foo.py:42: TODO handle timeout in stream reader",
        ),
    ]
    candidates = [
        {  # kept: cites signal content
            "title": "修复 foo.py 的超时 TODO",
            "goal": "处理 codepilot/foo.py:42 标注的超时问题。确保下游读完整数据。",
            "priority": "P3",
            "rationale": "TODO 明确指向具体行。",
            "kind": "bug",
            "evidence": "signal 3: codepilot/foo.py:42 TODO handle timeout",
            "effort": "small",
        },
        {  # dropped: generic filler
            "title": "通用优化",
            "goal": "补一下文档，加点日志。",
            "priority": "P3",
            "rationale": "看起来可以做。",
            "kind": "chore",
            "evidence": "signal 3: codepilot/foo.py:42",
            "effort": "small",
        },
        {  # dropped: title too long (>40 Chinese chars)
            "title": "给系统的全部模块逐一补齐文档说明并且梳理依赖关系让新人也能看明白这块复杂业务逻辑的前因后果",
            "goal": "人肉补注释。",
            "priority": "P3",
            "rationale": "reasonable",
            "kind": "docs",
            "evidence": "signal 3: codepilot/foo.py:42",
            "effort": "medium",
        },
        {  # dropped: no evidence
            "title": "改造 bar 模块",
            "goal": "让它支持并发调用。",
            "priority": "P2",
            "rationale": "想到的。",
            "kind": "refactor",
            "evidence": "",
            "effort": "medium",
        },
        {  # dropped: evidence not grounded in any signal
            "title": "新增审计日志管线",
            "goal": "引入集中审计日志。",
            "priority": "P3",
            "rationale": "安全性考虑。",
            "kind": "feat",
            "evidence": "上次线上故障后的讨论记录",
            "effort": "large",
        },
    ]

    kept, dropped = inspect_cmd._filter_candidates(candidates, signal_results=signal_results)
    reasons = {item["reason"] for item in dropped}

    assert len(kept) == 1
    assert kept[0]["title"] == "修复 foo.py 的超时 TODO"
    assert kept[0]["files"] == ["codepilot/foo.py"]
    assert reasons == {"generic_filler", "title_too_long", "missing_evidence", "evidence_not_grounded"}


def test_filter_candidates_drops_items_without_real_file_paths():
    signal_results = [
        inspect_cmd.InspectSignalResult(
            key="todos",
            title="代码里的 TODO/FIXME/XXX",
            order=3,
            enabled=True,
            content="signal text says timeout handling is incomplete",
        ),
    ]

    kept, dropped = inspect_cmd._filter_candidates(
        [
            {
                "title": "修复超时处理",
                "goal": "处理巡检发现的超时问题。",
                "priority": "P3",
                "rationale": "TODO 指向不完整处理。",
                "kind": "bug",
                "evidence": "signal 3: timeout handling is incomplete",
                "effort": "small",
            }
        ],
        signal_results=signal_results,
    )

    assert kept == []
    assert dropped == [{"title": "修复超时处理", "reason": "missing_files"}]


def test_build_content_surfaces_evidence_and_effort():
    content = inspect_cmd._build_content({
        "title": "修复 foo.py 的超时 TODO",
        "goal": "处理 codepilot/foo.py:42 的 TODO。",
        "rationale": "TODO 指向明确行。",
        "kind": "bug",
        "evidence": "signal 3: codepilot/foo.py:42",
        "effort": "small",
        "files": ["codepilot/foo.py"],
        "acceptance_criteria": ["codepilot/foo.py:42 指向的超时 TODO 已被消除。"],
        "verification_commands": ["pytest -n auto --dist loadfile -m \"not slow\" tests/test_foo.py -q"],
    })
    assert "Agent | codex" in content
    assert "Priority | P3" in content
    assert "kind=bug" in content
    assert "effort=small" in content
    assert "## Files In Scope" in content
    assert "- codepilot/foo.py" in content
    assert "待确认" not in content
    assert "## Planning Evidence" in content
    assert "## Reviewer Checkpoints" in content
    assert "codepilot/foo.py:42" in content
    assert "pytest -n auto --dist loadfile -m \"not slow\" tests/test_foo.py -q" in content
    assert missing_task_template_sections(content) == []


def test_materialize_inspection_output_skips_candidates_without_existing_files(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda project_name: set())
    monkeypatch.setattr(inspect_cmd.db, "list_tasks", lambda **kwargs: [])

    def _should_not_write(**kwargs):
        raise AssertionError("candidate without a real file must not be written")

    monkeypatch.setattr(inspect_cmd.db, "create_task", _should_not_write)

    created, skipped = inspect_cmd._materialize_inspection_output(
        [
            {
                "title": "修复 missing.py",
                "goal": "处理 missing.py 的 TODO。",
                "priority": "P2",
                "rationale": "TODO 指向文件。",
                "kind": "bug",
                "evidence": "signal 3: missing.py:1 TODO handle timeout",
                "effort": "small",
            }
        ],
        max_new_tasks=1,
        project_name="demo",
        project_path=project,
        priority="P3",
        agent="codex",
        dry_run=False,
    )

    assert created == []
    assert skipped == [{"title": "修复 missing.py", "reason": "files_not_found"}]


def test_materialize_inspection_output_writes_task_template_content(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    project = Path(tmp_path)
    (project / "codepilot").mkdir()
    (project / "codepilot" / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda project_name: set())
    monkeypatch.setattr(inspect_cmd.db, "list_tasks", lambda **kwargs: [])

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"id": 1, **kwargs}

    monkeypatch.setattr(inspect_cmd.db, "create_task", fake_create_task)

    created, skipped = inspect_cmd._materialize_inspection_output(
        [
            {
                "title": "修复 foo.py 的超时 TODO",
                "goal": "处理 codepilot/foo.py:42 的 TODO。",
                "priority": "P1",
                "rationale": "TODO 指向明确行。",
                "kind": "bug",
                "evidence": "signal 3: codepilot/foo.py:42",
                "effort": "small",
                "files": ["codepilot/foo.py"],
                "acceptance_criteria": ["codepilot/foo.py:42 指向的 TODO 已处理。"],
                "verification_commands": ["pytest -n auto --dist loadfile -m \"not slow\" tests/test_foo.py -q"],
            }
        ],
        max_new_tasks=1,
        project_name="demo",
        project_path=project,
        priority="P3",
        agent="claude-sonnet",
        dry_run=False,
    )

    assert len(created) == 1
    assert skipped == []
    assert captured["agent"] == "claude-sonnet"
    assert captured["priority"] == "P1"
    content = str(captured["content"])
    assert "Agent | claude-sonnet" in content
    assert "Priority | P1" in content
    assert "## Task Goal" in content
    assert "- codepilot/foo.py" in content
    assert "## Planning Evidence" in content
    assert "pytest -n auto --dist loadfile -m \"not slow\" tests/test_foo.py -q" in content
    assert "待确认" not in content
    assert missing_task_template_sections(content) == []


def test_inspect_json_mode_outputs_only_contract_stdout(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project = tmp_path / "demo"
    project.mkdir()
    (project / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")
    db.register_project("demo", str(project))

    cfg = SimpleNamespace(
        inspect=SimpleNamespace(
            max_new_tasks_per_round=1,
            interval_seconds=1,
            signals=("todos",),
            priority="P3",
            auto_execute=False,
        ),
        automation=SimpleNamespace(agent_language="en"),
    )
    monkeypatch.setattr(inspect_cmd, "load_project_config", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(inspect_cmd, "resolve_planner", lambda *_args, **_kwargs: "codex")
    monkeypatch.setattr(inspect_cmd.db, "list_tasks", lambda **kwargs: [])
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda project_name: set())
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

    def _fake_call_llm(*_args, **kwargs):
        callback = kwargs.get("stream_callback")
        if callback:
            callback("[planner] streamed noise")
        return {
            "candidates": [
                {
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py:1 的 TODO，避免超时路径继续缺实现。",
                    "priority": "P3",
                    "rationale": "TODO 指向明确文件。",
                    "kind": "bug",
                    "evidence": "signal 3: foo.py:1 TODO handle timeout",
                    "effort": "small",
                    "files": ["foo.py"],
                    "acceptance_criteria": ["foo.py:1 的超时 TODO 已处理。"],
                    "verification_commands": ["pytest -n auto --dist loadfile -m \"not slow\" -q"],
                }
            ]
        }

    monkeypatch.setattr(inspect_cmd, "_call_llm", _fake_call_llm)

    result = CliRunner().invoke(main, ["inspect", "-p", "demo", "--once", "--dry-run", "--json", "--max", "1"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["command"] == "inspect"
    assert payload["ok"] is True
    assert payload["data"]["created"][0]["files"] == ["foo.py"]
    assert "[planner]" not in result.output
    assert "streamed noise" not in result.output


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
