from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.agent_support import ai_guide_markdown, command_manifest
from codepilot import binary as binary_mod
from codepilot import binary_paths as binary_paths_mod
from codepilot import db
from codepilot import ai as ai_mod
from codepilot import progress_bus
from codepilot.ai_gateway import GatewayResponse
from codepilot import runtime as runtime_mod
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.config import load_project_config
from tests.workflow_testkit import init_test_db as _init_test_db


def test_create_task_dedup_returns_existing_backlog_task(tmp_path, monkeypatch):
    """Submitting the same project+title+content while a backlog task exists returns
    the original task id instead of creating a duplicate."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    second = db.create_task("demo", "implement feature X", content="details")

    assert first["id"] == second["id"]
    assert first["dedup_key"] == second["dedup_key"]
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 1


def test_create_task_dedup_allows_resubmit_after_done(tmp_path, monkeypatch):
    """A done task with the same dedup_key should NOT block creating a new task."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    db.update_task(first["id"], status="done")

    second = db.create_task("demo", "implement feature X", content="details")

    assert second["id"] != first["id"]
    assert second["dedup_key"] == first["dedup_key"]


def test_create_task_dedup_allows_resubmit_after_failed(tmp_path, monkeypatch):
    """A failed task with the same dedup_key should NOT block creating a new task."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    db.update_task(first["id"], status="failed")

    second = db.create_task("demo", "implement feature X", content="details")

    assert second["id"] != first["id"]


def test_create_task_dedup_blocks_in_progress_duplicate(tmp_path, monkeypatch):
    """An in_progress task with the same dedup_key should block creating a duplicate."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    db.update_task(first["id"], status="in_progress")

    second = db.create_task("demo", "implement feature X", content="details")

    assert second["id"] == first["id"]


def test_create_task_dedup_prints_notice(tmp_path, monkeypatch, capsys):
    """When returning an existing task, create_task prints [i] 已存在任务 #N."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X")
    db.create_task("demo", "implement feature X")

    captured = capsys.readouterr()
    assert f"[i] 已存在任务 #{first['id']}" in captured.out


def test_create_task_respects_caller_supplied_dedup_key(tmp_path, monkeypatch):
    """When a caller provides an explicit dedup_key it is used as-is."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "task A", dedup_key="custom-key-1234")
    second = db.create_task("demo", "task B", dedup_key="custom-key-1234")

    assert first["id"] == second["id"]
    assert first["dedup_key"] == "custom-key-1234"
