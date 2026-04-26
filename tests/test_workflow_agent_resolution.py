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


def test_normalize_agent_name_preserves_dual():
    assert ai_mod.normalize_agent_name("dual") == "dual"


def test_generate_task_content_uses_codex_for_dual(monkeypatch):
    captured = {}

    monkeypatch.setattr(ai_mod, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(ai_mod, "_collect_project_context", lambda project_path: "")

    def fake_run_cli_provider(provider, prompt, env_overrides=None):
        captured["provider"] = provider.name
        captured["prompt"] = prompt
        return "generated"

    monkeypatch.setattr(ai_mod, "_run_cli_provider", fake_run_cli_provider)

    content = ai_mod.generate_task_content("实现一个自动重试机制", agent="dual")

    assert content == "generated"
    assert captured["provider"] == "OpenAI Codex"
    assert "实现一个自动重试机制" in captured["prompt"]


def test_resolve_task_agent_preserves_dual(monkeypatch):
    monkeypatch.setattr(auto_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(auto_cmd, "_project_config", lambda project_info: None)

    resolved = auto_cmd._resolve_task_agent({"default_mode": "dual", "path": "D:/demo"}, "dual", "builtin")

    assert resolved == "dual"


def test_resolve_task_agent_prefers_automation_task_agent(monkeypatch):
    from codepilot.core.config import AgentsConfig

    cfg = AgentsConfig.from_dict({
        "project": {"default_mode": "dual"},
        "automation": {"task_agent": "claude"},
    })

    monkeypatch.setattr(auto_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(auto_cmd, "_project_config", lambda project_info: cfg)

    resolved = auto_cmd._resolve_task_agent({"default_mode": "dual", "path": "D:/demo"}, None, "builtin")

    assert resolved == "claude"


def test_resolve_builtin_phase_agent_uses_dual_split():
    assert run_cmd._resolve_builtin_phase_agent("dual", "builder") == ("codex", None)
    assert run_cmd._resolve_builtin_phase_agent("dual", "reviewer") == ("claude", None)


def test_resolve_builtin_phase_agent_uses_configured_dual_split(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = "sonnet"
reviewer = "codex"
""".strip(),
        encoding="utf-8",
    )

    assert run_cmd._resolve_builtin_phase_agent("dual", "builder", project_ref=project_path) == ("claude", "sonnet")
    assert run_cmd._resolve_builtin_phase_agent("dual", "reviewer", project_ref=project_path) == ("codex", None)


def test_agents_config_reads_and_normalizes_dual_phase_agents(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = " codex "
reviewer = " sonnet "
""".strip(),
        encoding="utf-8",
    )

    cfg = load_project_config(project_path)

    assert cfg is not None
    assert cfg.builder == "codex"
    assert cfg.reviewer == "claude-sonnet"


def test_agents_config_treats_blank_dual_phase_agents_as_unset(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = "   "
reviewer = ""
codex_cmd = "codex"
claude_cmd = "claude"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_project_config(project_path)

    assert cfg is not None
    assert cfg.builder is None
    assert cfg.reviewer is None
    assert cfg.codex_cmd == "codex"
    assert cfg.claude_cmd == "claude"

