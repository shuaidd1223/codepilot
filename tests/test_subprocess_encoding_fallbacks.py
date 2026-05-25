from __future__ import annotations

from dataclasses import dataclass

import pytest

from codepilot.ai_support import planner_context as planner_context
from codepilot.ai_support import providers as ai_providers
from codepilot.commands import run_shell


@dataclass
class _ProcessResult:
    returncode: int = 0
    stdout: str | bytes = b""
    stderr: str | bytes = b""


def test_run_cli_provider_decodes_non_utf8_stderr(tmp_path, monkeypatch):
    fake_exe = tmp_path / "codex.cmd"
    fake_exe.write_text("@echo off\n", encoding="utf-8")
    provider = ai_providers.CLIProvider(name="Codex", cmd=str(fake_exe), args_template=["exec", "{prompt}"], timeout=10)

    monkeypatch.setattr(
        ai_providers.subprocess,
        "run",
        lambda *args, **kwargs: _ProcessResult(
            returncode=1,
            stdout=b"",
            stderr="系统繁忙，请稍后重试".encode("gb18030"),
        ),
    )

    with pytest.raises(RuntimeError) as exc:
        ai_providers._run_cli_provider(provider, "hello")

    assert "系统繁忙" in str(exc.value)


def test_run_command_decodes_non_utf8_output(monkeypatch):
    captured: dict[str, bytes] = {}

    def _fake_run(*_args, **kwargs):
        captured["input"] = kwargs.get("input")
        return _ProcessResult(
            returncode=0,
            stdout="执行完成".encode("gb18030"),
            stderr="",
        )

    monkeypatch.setattr(run_shell.subprocess, "run", _fake_run)

    code, output = run_shell._run_command(["fake", "cmd"], input_text="你好")

    assert code == 0
    assert "执行完成" in output
    assert captured["input"] == "你好".encode("utf-8")


def test_run_git_decodes_non_utf8_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(
        planner_context.subprocess,
        "run",
        lambda *args, **kwargs: _ProcessResult(
            returncode=0,
            stdout="修复编码问题".encode("gb18030"),
            stderr=b"",
        ),
    )

    text = planner_context._run_git(["log", "-n1"], tmp_path)
    assert text == "修复编码问题"

