from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from codepilot.core.config import AgentsConfig
from codepilot.scheduled.runner import run_agent_job


@dataclass
class Completed:
    args: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _job(agent: str) -> dict[str, Any]:
    return {
        "type": "agent_job",
        "name": "hourly_health",
        "project": "demo",
        "agent": agent,
        "prompt": "Check project health.",
        "trigger": {"type": "interval", "interval": "1h"},
    }


def _fake_run_factory(calls: list[dict[str, Any]]):
    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append({"args": list(args), **kwargs})
        payload = (
            '{"tool_calls":[{"name":"Read"},{"name":"Bash"}],'
            '"usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18},'
            '"cost":0.012}'
        )
        return Completed(args=list(args), stdout=f"done\n{payload}\n")

    return fake_run


def test_run_agent_job_builds_headless_commands_for_cli_families(tmp_path: Path):
    calls: list[dict[str, Any]] = []

    results = [
        run_agent_job(
            _job(agent),
            project_root=tmp_path,
            dry_run=False,
            subprocess_run=_fake_run_factory(calls),
            commands={agent: f"{agent}-bin"},
        )
        for agent in ("claude", "codex", "opencode")
    ]

    assert calls[0]["args"] == [
        "claude-bin",
        "-p",
        "Please respond in English.\n\nTask:\nCheck project health.",
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]
    assert calls[1]["args"] == [
        "codex-bin",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "Please respond in English.\n\nTask:\nCheck project health.",
    ]
    assert calls[2]["args"] == ["opencode-bin", "run", "Please respond in English.\n\nTask:\nCheck project health."]
    assert [call.get("input") for call in calls] == [None, None, None]
    assert [result.exit_code for result in results] == [0, 0, 0]
    assert results[0].stdout.startswith("done")
    assert results[0].tool_call_count == 2
    assert results[0].token_usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
    assert results[0].cost == 0.012


def test_run_agent_job_injects_mcp_servers_via_environment(tmp_path: Path):
    calls: list[dict[str, Any]] = []

    run_agent_job(
        _job("codex"),
        project_root=tmp_path,
        dry_run=False,
        subprocess_run=_fake_run_factory(calls),
        commands={"codex": "codex-bin"},
        mcp_servers={
            "filesystem": {
                "command": "node",
                "args": ["server.js"],
                "env": {"ROOT": str(tmp_path)},
            }
        },
    )

    env = calls[0]["env"]
    assert "CODEPILOT_MCP_SERVERS" in env
    assert '"filesystem"' in env["CODEPILOT_MCP_SERVERS"]
    assert '"server.js"' in env["CODEPILOT_MCP_SERVERS"]


def test_run_agent_job_injects_family_runtime_env_from_config(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    calls: list[dict[str, Any]] = []
    cfg = AgentsConfig.from_dict({"providers": {"openai-gpt4o": {"api_key": "sk-test"}}})

    run_agent_job(
        _job("codex"),
        project_root=tmp_path,
        dry_run=False,
        subprocess_run=_fake_run_factory(calls),
        commands={"codex": "codex-bin"},
        config=cfg,
    )

    assert calls[0]["env"]["OPENAI_API_KEY"] == "sk-test"


def test_run_agent_job_can_wrap_prompt_for_chinese_agent_language(tmp_path: Path):
    calls: list[dict[str, Any]] = []
    cfg = AgentsConfig.from_dict({"automation": {"agent_language": "zh-CN"}})

    run_agent_job(
        _job("codex"),
        project_root=tmp_path,
        dry_run=False,
        subprocess_run=_fake_run_factory(calls),
        commands={"codex": "codex-bin"},
        config=cfg,
    )

    assert calls[0]["args"][-1] == "请使用简体中文输出。\n\n任务：\nCheck project health."


def test_run_agent_job_dry_run_returns_command_without_subprocess(tmp_path: Path):
    calls: list[dict[str, Any]] = []

    result = run_agent_job(
        _job("codex"),
        project_root=tmp_path,
        dry_run=True,
        subprocess_run=_fake_run_factory(calls),
        commands={"codex": "codex-bin"},
    )

    assert calls == []
    assert result.dry_run is True
    assert result.exit_code is None
    assert result.command[0] == "codex-bin"


def test_run_agent_job_audits_nonzero_exit_code(tmp_path: Path):
    def fake_run(args, **kwargs):  # noqa: ANN001
        return Completed(
            args=list(args),
            returncode=2,
            stdout="",
            stderr='failed\n{"tools":[{"name":"Bash"}],"cost":0.02}\n',
        )

    result = run_agent_job(
        _job("claude"),
        project_root=tmp_path,
        dry_run=False,
        subprocess_run=fake_run,
        commands={"claude": "claude-bin"},
    )

    audit_path = tmp_path / ".codepilot" / "scheduled" / "audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert result.exit_code == 2
    assert "failed" in result.stderr
    assert record["exit_code"] == 2
    assert record["tools"] == [{"name": "Bash"}]
    assert record["cost"] == 0.02


def test_run_agent_job_captures_bytes_and_decodes_with_fallback(tmp_path: Path):
    def fake_run(args, **kwargs):  # noqa: ANN001
        assert kwargs["text"] is False
        return Completed(
            args=list(args),
            returncode=0,
            stdout="中文输出".encode("gb18030"),
            stderr=b"",
        )

    result = run_agent_job(
        _job("codex"),
        project_root=tmp_path,
        dry_run=False,
        subprocess_run=fake_run,
        commands={"codex": "codex-bin"},
    )

    assert "中文输出" in result.stdout
