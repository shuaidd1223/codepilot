from __future__ import annotations

import inspect as py_inspect
import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from codepilot.ai_support import agent_support
from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from codepilot.commands import inspect as inspect_cmd
from codepilot.storage import database as db
from tests.ai_gateway_testkit import FakeCLIProvider, StreamingSchemaSubprocess
from tests.workflow_testkit import init_test_db


def test_agent_support_is_thin_compatibility_facade():
    source_lines = Path(agent_support.__file__).read_text(encoding="utf-8").splitlines()

    assert len(source_lines) <= 80
    assert py_inspect.getmodule(agent_support.command_manifest).__name__ == "codepilot.ai_support.agent_manifest"
    assert py_inspect.getmodule(agent_support.ai_guide_markdown).__name__ == "codepilot.ai_support.agent_guides"
    assert py_inspect.getmodule(agent_support.ai_prompt_text).__name__ == "codepilot.ai_support.agent_guides"
    assert py_inspect.getmodule(agent_support.task_template_schema).__name__ == "codepilot.ai_support.agent_task_template"


def _register_demo_project(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    registered = db.register_project("demo", str(project))
    cfg = SimpleNamespace(
        inspect=SimpleNamespace(
            max_new_tasks_per_round=3,
            interval_seconds=1800,
            signals=("todos",),
            priority="P3",
            auto_execute=False,
        )
    )
    monkeypatch.setattr(inspect_cmd, "load_project_config", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(inspect_cmd, "resolve_planner", lambda _cfg, _kind, explicit=None: explicit or "codex")
    return registered


def _substantive_signal() -> inspect_cmd.InspectSignalResult:
    return inspect_cmd.InspectSignalResult(
        key="todos",
        title="代码里的 TODO/FIXME/XXX",
        order=3,
        enabled=True,
        content="codepilot/foo.py:12 TODO: split the inspect stream renderer",
    )


def test_inspect_renders_stream_chunk_before_final_output_and_keeps_dry_run_read_only(tmp_path, monkeypatch):
    _register_demo_project(tmp_path, monkeypatch)
    before_total = db.get_task_stats("demo")["total"]
    monkeypatch.setattr(
        inspect_cmd,
        "collect_inspection_signal_results",
        lambda *_args, **_kwargs: [_substantive_signal()],
    )

    def fake_codex_schema_prompt(_prompt, _schema, **kwargs):
        stream_callback = kwargs.get("stream_callback")
        assert stream_callback is not None
        stream_callback("S")
        return {"candidates": []}

    monkeypatch.setattr(inspect_cmd, "_run_codex_schema_prompt", fake_codex_schema_prompt)

    result = CliRunner().invoke(
        inspect_cmd.inspect,
        ["-p", "demo", "--once", "--dry-run", "--planner", "codex"],
    )

    assert result.exit_code == 0, result.output
    assert "S" in result.output
    assert "候选总数" in result.output
    assert result.output.index("S") < result.output.index("候选总数")
    assert db.get_task_stats("demo")["total"] == before_total


def test_inspect_json_suppresses_stream_chunks_and_remains_parseable(tmp_path, monkeypatch):
    _register_demo_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        inspect_cmd,
        "collect_inspection_signal_results",
        lambda *_args, **_kwargs: [_substantive_signal()],
    )

    def fake_codex_schema_prompt(_prompt, _schema, **kwargs):
        assert kwargs.get("stream_callback") is None
        return {"candidates": []}

    monkeypatch.setattr(inspect_cmd, "_run_codex_schema_prompt", fake_codex_schema_prompt)

    result = CliRunner().invoke(
        inspect_cmd.inspect,
        ["-p", "demo", "--once", "--dry-run", "--planner", "codex", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "inspect"
    assert payload["data"]["candidates_total"] == 0


def test_inspect_keeps_streaming_cli_fallback_without_legacy_classifier(tmp_path, monkeypatch):
    _register_demo_project(tmp_path, monkeypatch)
    cfg = SimpleNamespace(
        inspect=SimpleNamespace(
            max_new_tasks_per_round=3,
            interval_seconds=1800,
            signals=("todos",),
            priority="P3",
            auto_execute=False,
        ),
        classifier=SimpleNamespace(
            enabled=True,
            provider="openai",
            model="gpt-test",
            timeout=30,
        ),
        providers={},
        get_provider_api_key=lambda _provider: "",
    )
    monkeypatch.setattr(inspect_cmd, "load_project_config", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(
        inspect_cmd,
        "collect_inspection_signal_results",
        lambda *_args, **_kwargs: [_substantive_signal()],
    )
    captured: dict[str, object] = {}

    def fake_call_llm(_prompt, **kwargs):
        captured.update(kwargs)
        stream_callback = kwargs.get("stream_callback")
        assert stream_callback is not None
        stream_callback("L")
        return {"candidates": []}

    monkeypatch.setattr(inspect_cmd, "_call_llm", fake_call_llm)

    result = CliRunner().invoke(
        inspect_cmd.inspect,
        ["-p", "demo", "--once", "--dry-run", "--planner", "codex"],
    )

    assert result.exit_code == 0, result.output
    assert captured["classifier_provider"] == ""
    assert captured["classifier_model"] == ""
    assert "L" in result.output
    assert "候选总数" in result.output
    assert result.output.index("L") < result.output.index("候选总数")


def test_inspect_rejects_removed_legacy_classifier_option(tmp_path, monkeypatch):
    _register_demo_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        inspect_cmd.inspect,
        ["-p", "demo", "--once", "--dry-run", "--legacy-classifier"],
    )

    assert result.exit_code != 0
    assert "No such option: --legacy-classifier" in result.output
