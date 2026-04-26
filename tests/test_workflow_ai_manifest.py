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


def test_ai_manifest_command_outputs_machine_readable_json():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "manifest"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["name"] == "CodePilot"
    assert any(command["name"] == "status" for command in payload["commands"])
    assert any(item["command"] == f"{payload['command_name']} ai manifest" for item in payload["structured_outputs"])


def test_ai_manifest_command_allows_version_and_command_override():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ai", "manifest", "--version", "9.9.9", "--command-name", "mypilot", "--binary-name", "mypilot"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    binary_release = next(item for item in payload["commands"] if item["name"] == "binary_release")
    assert payload["version"] == "9.9.9"
    assert payload["command_name"] == "mypilot"
    assert payload["structured_outputs"][0]["command"] == "mypilot ai manifest"
    assert payload["commands"][0]["syntax"] == "mypilot init <path>"
    assert "dist/binary/linux-x86_64/mypilot" in binary_release["examples"][1]


def test_repo_ai_manifest_file_stays_in_sync():
    manifest_path = Path(__file__).resolve().parents[1] / "AI_MANIFEST.json"

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload == command_manifest()


def test_repo_ai_usage_file_stays_in_sync():
    guide_path = Path(__file__).resolve().parents[1] / "AI_USAGE.zh-CN.md"

    assert guide_path.read_text(encoding="utf-8") == ai_guide_markdown()


def test_ai_guide_command_outputs_markdown_usage():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "guide"])

    assert result.exit_code == 0
    assert "# CodePilot AI 调用手册" in result.output
    assert "codepilot status -p <项目名> --json" in result.output
    assert "codepilot ai manifest" in result.output


def test_ai_prompt_command_outputs_short_agent_prompt():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "prompt"])

    assert result.exit_code == 0
    assert "codepilot \"需求文本\"" in result.output
    assert "codepilot binary prepare --version <版本号>" in result.output


def test_ai_template_default_outputs_raw_markdown():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template"])

    assert result.exit_code == 0
    assert "{title}" in result.output
    assert "{goal}" in result.output
    assert "{evidence}" in result.output
    assert "Planning Evidence" in result.output


def test_ai_template_json_returns_structured_schema():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "template_markdown" in payload
    assert payload["language"]["scaffolding"] == "English"
    assert payload["language"]["placeholders"] == "Chinese"

    placeholder_names = {item["name"] for item in payload["placeholders"]}
    for required in ("title", "goal", "evidence", "criteria", "ac_matrix"):
        assert required in placeholder_names

    batch = payload["batch_import"]
    assert "fields" in batch and len(batch["fields"]) >= 3
    assert any(f["name"] == "content" for f in batch["fields"])
    assert isinstance(batch["example"], list) and batch["example"]
    assert payload["validation"]["required_headings"]
    assert "{goal}" in payload["validation"]["placeholder_tokens"]


def test_ai_template_guide_renders_chinese_markdown():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template", "--format", "guide"])

    assert result.exit_code == 0
    assert "# CodePilot 任务模板填充指南" in result.output
    assert "模板占位符" in result.output
    assert "批量导入 JSON 格式" in result.output


def test_ai_template_command_name_override_propagates():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ai", "template", "--format", "json", "--command-name", "mypilot"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["batch_import"]["command"].startswith("mypilot add")
    assert any(cmd.startswith("mypilot ") for cmd in payload["see_also"])


def test_manifest_advertises_ai_template_command():
    payload = command_manifest()
    names = {c["name"] for c in payload["commands"]}
    assert "ai_template" in names

    outputs = [o["command"] for o in payload["structured_outputs"]]
    assert any("ai template --format json" in cmd for cmd in outputs)

