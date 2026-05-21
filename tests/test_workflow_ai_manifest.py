from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile
import re

import click
import pytest
from click.testing import CliRunner

from codepilot.ai_support.agent_support import ai_guide_markdown, command_manifest
from codepilot.binary_support import manager as binary_mod
from codepilot.binary_support import paths as binary_paths_mod
from codepilot.core.task_template import missing_task_template_sections, unreplaced_task_template_placeholders
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


def test_ai_manifest_includes_project_metadata():
    payload = command_manifest()

    assert payload["author"] == {"name": "帅呆呆", "email": "2264505396@qq.com"}
    assert payload["repository"] == "https://gitee.com/shuai_dd/CodePilot"
    assert payload["license"] == "MIT"


def test_repo_ai_usage_file_stays_in_sync():
    guide_path = Path(__file__).resolve().parents[1] / "AI_USAGE.zh-CN.md"

    assert guide_path.read_text(encoding="utf-8") == ai_guide_markdown(language="zh-CN")


def test_repo_ai_usage_english_file_stays_in_sync():
    guide_path = Path(__file__).resolve().parents[1] / "AI_USAGE.en-US.md"

    assert guide_path.read_text(encoding="utf-8") == ai_guide_markdown(language="en")


def test_ai_usage_guides_include_project_metadata_and_next_actions_contract():
    zh_guide = ai_guide_markdown(language="zh-CN")
    en_guide = ai_guide_markdown(language="en")

    assert "**作者：** 帅呆呆 <2264505396@qq.com>" in zh_guide
    assert "**Author:** 帅呆呆 <2264505396@qq.com>" in en_guide
    assert "next_actions" in zh_guide
    assert "suggested_command" in zh_guide
    assert "这些是建议，不会自动执行" in zh_guide


def test_active_explanation_docs_are_bilingual_and_linked():
    root = Path(__file__).resolve().parents[1]
    pairs = [
        ("README.md", "README.en-US.md"),
        ("docs/说明文档.zh-CN.md", "docs/说明文档.en-US.md"),
        ("docs/操作文档.zh-CN.md", "docs/操作文档.en-US.md"),
        ("docs/AI与Agent调用手册.zh-CN.md", "docs/AI与Agent调用手册.en-US.md"),
        ("docs/Skill化集成指南.zh-CN.md", "docs/Skill化集成指南.en-US.md"),
        ("docs/workflow-state.zh-CN.md", "docs/workflow-state.en-US.md"),
        ("docs/project-services.md", "docs/project-services.en-US.md"),
        ("AI_USAGE.zh-CN.md", "AI_USAGE.en-US.md"),
    ]

    for zh_rel, en_rel in pairs:
        zh_path = root / zh_rel
        en_path = root / en_rel
        assert zh_path.exists(), zh_rel
        assert en_path.exists(), en_rel
        zh_text = zh_path.read_text(encoding="utf-8")
        en_text = en_path.read_text(encoding="utf-8")
        assert Path(en_rel).name in zh_text
        assert Path(zh_rel).name in en_text


def test_readme_navigation_lists_bilingual_active_docs():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    readme_en = (root / "README.en-US.md").read_text(encoding="utf-8")
    expected_links = [
        "docs/说明文档.zh-CN.md",
        "docs/说明文档.en-US.md",
        "docs/操作文档.zh-CN.md",
        "docs/操作文档.en-US.md",
        "docs/AI与Agent调用手册.zh-CN.md",
        "docs/AI与Agent调用手册.en-US.md",
        "docs/Skill化集成指南.zh-CN.md",
        "docs/Skill化集成指南.en-US.md",
        "docs/project-services.md",
        "docs/project-services.en-US.md",
        "AI_USAGE.zh-CN.md",
        "AI_USAGE.en-US.md",
    ]

    for link in expected_links:
        assert link in readme
        assert link in readme_en


def test_ai_guide_command_outputs_markdown_usage():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "guide"])

    assert result.exit_code == 0
    assert "# CodePilot AI Usage Guide" in result.output
    assert "codepilot status -p <project-name> --json" in result.output
    assert "codepilot ai manifest" in result.output


def test_ai_guide_command_can_output_chinese_markdown_usage():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "guide", "--language", "zh-CN"])

    assert result.exit_code == 0
    assert "# CodePilot AI 调用手册" in result.output
    assert "codepilot status -p <项目名> --json" in result.output
    assert "codepilot ai manifest" in result.output


def test_ai_prompt_command_outputs_short_agent_prompt():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "prompt"])

    assert result.exit_code == 0
    assert "codepilot \"requirement text\"" in result.output
    assert "codepilot binary prepare --version <version>" in result.output


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
    assert payload["language"]["selected"] == "en"
    assert payload["language"]["supported"] == ["en", "zh-CN"]
    assert payload["language"]["scaffolding"] == "English"
    assert payload["language"]["placeholders"] == "English"

    placeholder_names = {item["name"] for item in payload["placeholders"]}
    for required in ("title", "goal", "evidence", "criteria", "ac_matrix"):
        assert required in placeholder_names

    batch = payload["batch_import"]
    assert "fields" in batch and len(batch["fields"]) >= 3
    assert any(f["name"] == "content" for f in batch["fields"])
    assert isinstance(batch["example"], list) and batch["example"]
    assert payload["validation"]["required_headings"]
    assert "{goal}" in payload["validation"]["placeholder_tokens"]


def test_ai_template_json_batch_example_is_import_ready():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    placeholder_names = [item["name"] for item in payload["placeholders"]]

    for item in payload["batch_import"]["example"]:
        assert item.get("title")
        assert item.get("content")
        assert missing_task_template_sections(item["content"]) == []
        assert unreplaced_task_template_placeholders(
            item["content"],
            placeholder_names=placeholder_names,
        ) == []


def test_ai_template_json_batch_example_matches_full_template_structure():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    template_headings = re.findall(r"^##\s+(.+?)\s*$", payload["template_markdown"], flags=re.MULTILINE)

    for item in payload["batch_import"]["example"]:
        content = item["content"]
        for heading in template_headings:
            assert f"## {heading}" in content


def test_ai_template_guide_renders_english_markdown_by_default():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template", "--format", "guide"])

    assert result.exit_code == 0
    assert "# CodePilot Task Template Filling Guide" in result.output
    assert "Template Placeholders" in result.output
    assert "Batch Import JSON Format" in result.output


def test_ai_template_guide_can_render_chinese_markdown():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template", "--format", "guide", "--language", "zh-CN"])

    assert result.exit_code == 0
    assert "# CodePilot 任务模板填充指南" in result.output
    assert "模板占位符" in result.output
    assert "批量导入 JSON 格式" in result.output


def test_ai_template_json_can_render_chinese_schema():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "template", "--format", "json", "--language", "zh-CN"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["language"]["selected"] == "zh-CN"
    assert payload["language"]["placeholders"] == "Chinese"
    assert "Builder 子进程日志实时推送" in payload["batch_import"]["example"][0]["title"]


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
    assert "exec" in names
    assert "hook" in names
    assert "skill" in names
    assert "self_update" in names

    outputs = [o["command"] for o in payload["structured_outputs"]]
    assert any("ai template --format json" in cmd for cmd in outputs)
    assert any("exec --provider codex --dry-run --json" in cmd for cmd in outputs)
    assert any("hook validate" in cmd for cmd in outputs)
    assert any("skill run" in cmd for cmd in outputs)
    assert any("self-update" in cmd and "--dry-run --json" in cmd for cmd in outputs)
