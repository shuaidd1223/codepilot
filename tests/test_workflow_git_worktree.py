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


def test_task_branch_name_uses_slug_and_fallback():
    assert run_cmd._task_branch_name(20, "Add API endpoint") == "feat/task-20-add-api-endpoint"
    assert run_cmd._task_branch_name(21, "实现中文能力") == "feat/task-21-task"


def test_task_worktree_path_uses_configured_relative_base(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": "var/worktrees",
    }

    resolved_base = run_cmd._resolve_project_worktree_base(project_info)
    worktree_path = run_cmd._task_worktree_path(project_info, task_id=7, title="Add API endpoint")

    assert resolved_base == (project_path / "var" / "worktrees").resolve()
    assert worktree_path == resolved_base / "task-7-add-api-endpoint"


def test_task_worktree_path_uses_project_first_default_base(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    project_path = tmp_path / "project"
    project_path.mkdir()
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": "",
    }

    resolved_base = run_cmd._resolve_project_worktree_base(project_info)
    worktree_path = run_cmd._task_worktree_path(project_info, task_id=7, title="Add API endpoint")

    assert resolved_base == tmp_path / "home" / ".codepilot" / "data" / "demo" / "worktrees"
    assert worktree_path == resolved_base / "task-7-add-api-endpoint"


def test_git_prepare_and_cleanup_task_worktree(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / ".gitignore").write_text(".env*\nnode_modules/\n", encoding="utf-8")
    (project_path / ".env").write_text("DATABASE_URL=postgres://local\n", encoding="utf-8")
    (project_path / "node_modules" / "demo").mkdir(parents=True)
    (project_path / "node_modules" / "demo" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=7, title="Add API endpoint")

    branch_name, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=7,
        title="Add API endpoint",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    assert branch_name == run_cmd._task_branch_name(7, "Add API endpoint")
    assert worktree_path == expected_path.resolve()
    assert worktree_path.exists()
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True
    assert (worktree_path / ".env").read_text(encoding="utf-8") == "DATABASE_URL=postgres://local\n"
    assert (worktree_path / "node_modules" / "demo" / "index.js").read_text(encoding="utf-8") == "module.exports = 1;\n"

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=worktree_path, timeout=30)
    assert code == 0
    assert output.strip() == branch_name

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=branch_name)

    assert worktree_path.exists() is False
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is False
    assert (project_path / "node_modules" / "demo" / "index.js").exists()

    code, output = run_cmd._run_command(["git", "branch", "--list", branch_name], cwd=project_path, timeout=30)
    assert code == 0
    assert branch_name not in output


def test_git_prepare_task_worktree_links_common_dependency_dirs(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / ".gitignore").write_text("vendor/\n.venv/\ncmake-build-debug/\n", encoding="utf-8")
    (project_path / "vendor" / "pkg").mkdir(parents=True)
    (project_path / "vendor" / "pkg" / "autoload.php").write_text("<?php\n", encoding="utf-8")
    (project_path / ".venv" / "pyvenv.cfg").parent.mkdir(parents=True)
    (project_path / ".venv" / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")
    (project_path / "cmake-build-debug" / "CMakeCache.txt").parent.mkdir(parents=True)
    (project_path / "cmake-build-debug" / "CMakeCache.txt").write_text("# cache\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    worktree_target = run_cmd._task_worktree_path(project_info, task_id=17, title="Common deps")

    branch_name, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=17,
        title="Common deps",
        base_branch=base_branch,
        worktree_path=worktree_target,
    )

    assert (worktree_path / "vendor" / "pkg" / "autoload.php").read_text(encoding="utf-8") == "<?php\n"
    assert (worktree_path / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8") == "home = python\n"
    assert (worktree_path / "cmake-build-debug" / "CMakeCache.txt").read_text(encoding="utf-8") == "# cache\n"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=branch_name)

    assert (project_path / "vendor" / "pkg" / "autoload.php").exists()
    assert (project_path / ".venv" / "pyvenv.cfg").exists()
    assert (project_path / "cmake-build-debug" / "CMakeCache.txt").exists()


def test_git_prepare_task_worktree_allows_existing_empty_dir(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / ".gitignore").write_text(".env*\n", encoding="utf-8")
    (project_path / ".env").write_text("DATABASE_URL=postgres://original\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=8, title="Existing empty dir")
    expected_path.mkdir(parents=True)

    branch_name, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=8,
        title="Existing empty dir",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    assert branch_name == run_cmd._task_branch_name(8, "Existing empty dir")
    assert worktree_path == expected_path.resolve()
    assert worktree_path.exists()
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=branch_name)
    assert worktree_path.exists() is False


def test_git_prepare_task_worktree_reuses_existing_task_worktree_without_reset(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / ".gitignore").write_text(".env*\n", encoding="utf-8")
    (project_path / ".env").write_text("DATABASE_URL=postgres://original\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=9, title="Reuse worktree")

    branch_name, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=9,
        title="Reuse worktree",
        base_branch=base_branch,
        worktree_path=expected_path,
    )
    dirty_file = worktree_path / "dirty.txt"
    dirty_file.write_text("preserve me", encoding="utf-8")
    worktree_env = worktree_path / ".env"
    assert worktree_env.read_text(encoding="utf-8") == "DATABASE_URL=postgres://original\n"
    worktree_env.write_text("DATABASE_URL=postgres://worktree\n", encoding="utf-8")
    (project_path / ".env").write_text("DATABASE_URL=postgres://changed\n", encoding="utf-8")
    (project_path / ".env.local").write_text("LOCAL_ONLY=1\n", encoding="utf-8")

    branch_name2, worktree_path2 = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=9,
        title="Reuse worktree",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    assert branch_name2 == branch_name
    assert worktree_path2 == worktree_path
    assert dirty_file.exists()
    assert dirty_file.read_text(encoding="utf-8") == "preserve me"
    assert worktree_env.read_text(encoding="utf-8") == "DATABASE_URL=postgres://worktree\n"
    assert (worktree_path / ".env.local").read_text(encoding="utf-8") == "LOCAL_ONLY=1\n"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=branch_name)


def test_git_prepare_task_worktree_attaches_existing_task_branch_without_reset(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    task_branch = run_cmd._task_branch_name(10, "Attach branch")
    subprocess.run(["git", "checkout", "-b", task_branch], cwd=project_path, capture_output=True, check=True)
    (project_path / "feature.txt").write_text("keep branch history\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "checkout", base_branch], cwd=project_path, capture_output=True, check=True)

    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=10, title="Attach branch")

    branch_name, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=10,
        title="Attach branch",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    assert branch_name == task_branch
    assert worktree_path == expected_path.resolve()
    assert (worktree_path / "feature.txt").read_text(encoding="utf-8") == "keep branch history\n"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=branch_name)


def test_git_prepare_task_worktree_requires_existing_base_branch(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=11, title="Missing base")

    with pytest.raises(RuntimeError, match="base_branch `missing` .*不存在"):
        run_cmd._git_prepare_task_worktree(
            project_path,
            task_id=11,
            title="Missing base",
            base_branch="missing",
            worktree_path=expected_path,
        )


def test_git_prepare_task_worktree_refuses_other_branch_on_same_path(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=7, title="Add API endpoint")
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/other", str(expected_path), base_branch],
        cwd=project_path,
        capture_output=True,
        check=True,
    )

    with pytest.raises(RuntimeError, match="已被其他分支占用"):
        run_cmd._git_prepare_task_worktree(
            project_path,
            task_id=7,
            title="Add API endpoint",
            base_branch=base_branch,
            worktree_path=expected_path,
        )

    assert run_cmd._git_worktree_exists(project_path, expected_path) is True
    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=expected_path, timeout=30)
    assert code == 0
    assert output.strip() == "feat/other"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=expected_path, task_branch="feat/other")


def test_git_prepare_task_worktree_refuses_same_task_branch_on_other_worktree(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    task_branch = run_cmd._task_branch_name(7, "Add API endpoint")
    occupied_path = worktree_base / "occupied"
    target_path = worktree_base / "target"
    subprocess.run(
        ["git", "worktree", "add", "-b", task_branch, str(occupied_path), base_branch],
        cwd=project_path,
        capture_output=True,
        check=True,
    )

    with pytest.raises(RuntimeError, match="任务分支已被其他 worktree 占用"):
        run_cmd._git_prepare_task_worktree(
            project_path,
            task_id=7,
            title="Add API endpoint",
            base_branch=base_branch,
            worktree_path=target_path,
        )

    assert run_cmd._git_worktree_exists(project_path, occupied_path) is True
    assert run_cmd._git_worktree_exists(project_path, target_path) is False
    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=occupied_path, task_branch=task_branch)


def test_git_cleanup_task_worktree_refuses_branch_mismatch(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    worktree_path = worktree_base / "task-7-add-api-endpoint"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/other", str(worktree_path), base_branch],
        cwd=project_path,
        capture_output=True,
        check=True,
    )

    with pytest.raises(RuntimeError, match="拒绝移除不属于任务分支"):
        run_cmd._git_cleanup_task_worktree(
            project_path,
            worktree_path=worktree_path,
            task_branch=run_cmd._task_branch_name(7, "Add API endpoint"),
        )

    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True
    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=worktree_path, timeout=30)
    assert code == 0
    assert output.strip() == "feat/other"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch="feat/other")


def test_git_cleanup_task_worktree_does_not_delete_branch_without_matching_worktree(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    task_branch = run_cmd._task_branch_name(7, "Add API endpoint")
    subprocess.run(["git", "branch", task_branch], cwd=project_path, capture_output=True, check=True)

    missing_path = tmp_path / "missing-worktree"
    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=missing_path, task_branch=task_branch)

    code, output = run_cmd._run_command(["git", "branch", "--list", task_branch], cwd=project_path, timeout=30)
    assert code == 0
    assert task_branch in output


def test_run_backlog_creates_task_branch_and_checks_out_base_after_merge(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "Add API endpoint", agent="dual", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin"),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert current["branch_name"] == expected_branch
    assert current["worktree_path"] == str(project_path.resolve())

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == base_branch

    code, output = run_cmd._run_command(["git", "branch", "--list", expected_branch], cwd=project_path, timeout=30)
    assert code == 0
    assert expected_branch not in output, "task branch should be deleted after merge"


@pytest.mark.slow
def test_run_backlog_requeues_when_merge_back_fails_with_uncommitted_changes(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "leave dirty file", agent="dual", max_retries=3)

    def _fake_executor(*args, **kwargs):
        execution_path = Path(kwargs["execution_path"])
        (execution_path / "dirty.txt").write_text("left dirty", encoding="utf-8")
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", _fake_executor)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1
    assert "回合并失败" in (current["error_message"] or "")
    assert current["worktree_path"] == str(project_path.resolve())
    assert (project_path / "dirty.txt").exists() is True

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == expected_branch


def test_run_backlog_uses_branch_workspace_when_configured(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        """
[automation]
task_workspace = "branch"
""".strip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "AGENTS.toml"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "project workspace", agent="dual", max_retries=2)
    captured = {}

    def _fake_executor(*args, **kwargs):
        captured["execution_path"] = Path(kwargs["execution_path"]).resolve()
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", _fake_executor)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert current["worktree_path"] == str(project_path.resolve())
    assert captured["execution_path"] == project_path.resolve()
    assert run_cmd._git_list_worktrees(project_path) == [project_path.resolve()]


def test_run_backlog_defaults_to_branch_workspace_for_invalid_config(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        """
[automation]
task_workspace = "bad-value"
""".strip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "AGENTS.toml"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "bad workspace config", agent="dual", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin"),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert current["branch_name"] == expected_branch
    assert current["worktree_path"] == str(project_path.resolve())
    assert run_cmd._git_list_worktrees(project_path) == [project_path.resolve()]


def test_run_backlog_uses_direct_workspace_when_configured(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        """
[automation]
task_workspace = "direct"
""".strip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "AGENTS.toml"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "direct workspace", agent="dual", max_retries=2)
    captured = {}

    def _fake_executor(*args, **kwargs):
        captured["execution_path"] = Path(kwargs["execution_path"]).resolve()
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", _fake_executor)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert current["branch_name"] == base_branch
    assert current["worktree_path"] == str(project_path.resolve())
    assert captured["execution_path"] == project_path.resolve()
    assert run_cmd._git_list_worktrees(project_path) == [project_path.resolve()]

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == base_branch


@pytest.mark.slow
def test_run_backlog_keeps_builtin_worktree_changes_isolated_before_merge(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        """
[automation]
task_workspace = "worktree"
""".strip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "AGENTS.toml"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "isolate worktree writes", agent="dual", max_retries=2)
    captured = {}

    def _fake_executor(*args, **kwargs):
        execution_path = Path(kwargs["execution_path"]).resolve()
        captured["execution_path"] = execution_path
        (execution_path / "isolated.txt").write_text("worktree only\n", encoding="utf-8")
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    def _fake_merge(project_root, **kwargs):
        execution_path = Path(kwargs["worktree_path"]).resolve()
        captured["merge_path"] = execution_path
        captured["main_branch"] = run_cmd._git_current_branch(project_root)
        captured["main_has_file"] = (project_root / "isolated.txt").exists()
        captured["worktree_has_file"] = (execution_path / "isolated.txt").exists()
        return "merge skipped for isolation test"

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", _fake_executor)
    monkeypatch.setattr(run_cmd, "_git_merge_task_worktree", _fake_merge)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert captured["execution_path"] != project_path.resolve()
    assert captured["merge_path"] == captured["execution_path"]
    assert captured["main_branch"] == base_branch
    assert captured["main_has_file"] is False
    assert captured["worktree_has_file"] is True
    assert current["worktree_path"] != str(project_path.resolve())
    assert (project_path / "isolated.txt").exists() is False

    run_cmd._git_cleanup_task_worktree(
        project_path,
        worktree_path=captured["execution_path"],
        task_branch=current["branch_name"],
    )


def test_git_merge_task_worktree_aborts_conflicted_merge_and_keeps_main_clean(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=12, title="Conflicting merge")
    task_branch, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=12,
        title="Conflicting merge",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    (worktree_path / "shared.txt").write_text("task branch\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=worktree_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "task change"], cwd=worktree_path, capture_output=True, check=True)

    (project_path / "shared.txt").write_text("base branch\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "base change"], cwd=project_path, capture_output=True, check=True)

    with pytest.raises(RuntimeError, match="已自动执行 `git merge --abort`"):
        run_cmd._git_merge_task_worktree(
            project_path,
            task_id=12,
            title="Conflicting merge",
            task_branch=task_branch,
            base_branch=base_branch,
            worktree_path=worktree_path,
        )

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == base_branch
    assert run_cmd._git_has_changes(project_path) is False
    merge_head_code, _ = run_cmd._run_command(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=project_path,
        timeout=30,
    )
    assert merge_head_code != 0
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True
    assert (project_path / "shared.txt").read_text(encoding="utf-8") == "base branch\n"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=task_branch)


def test_git_merge_task_worktree_keeps_success_when_cleanup_fails(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess
    from codepilot.commands import run_git as run_git_cmd

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=13, title="Cleanup failure")
    task_branch, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=13,
        title="Cleanup failure",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    (worktree_path / "shared.txt").write_text("merged change\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=worktree_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "task change"], cwd=worktree_path, capture_output=True, check=True)

    monkeypatch.setattr(
        run_git_cmd,
        "_git_cleanup_task_worktree",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup exploded")),
    )

    summary = run_cmd._git_merge_task_worktree(
        project_path,
        task_id=13,
        title="Cleanup failure",
        task_branch=task_branch,
        base_branch=base_branch,
        worktree_path=worktree_path,
    )

    assert "已合并" in summary
    assert "cleanup exploded" in summary
    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == base_branch
    assert (project_path / "shared.txt").read_text(encoding="utf-8") == "merged change\n"
    assert run_cmd._git_local_branch_exists(project_path, task_branch) is True
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=task_branch)


def test_run_backlog_requires_main_worktree_on_base_branch_for_builtin_worktree_mode(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        """
[automation]
task_workspace = "worktree"
""".strip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "AGENTS.toml"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "-b", "topic"], cwd=project_path, capture_output=True, check=True)

    db.register_project("demo", str(project_path), base_branch="master")
    task = db.create_task("demo", "needs base branch lock", agent="dual", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert "base_branch" in (current["error_message"] or "")
    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == "topic"


def test_run_backlog_falls_back_to_main_workspace_when_per_task_branch_disabled(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    subprocess.run(["git", "checkout", "-b", "topic"], cwd=project_path, capture_output=True, check=True)
    (project_path / "AGENTS.toml").write_text(
        f"""
[project]
name = "demo"
base_branch = "{base_branch}"

    [automation]
    per_task_branch = false
    """.strip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "AGENTS.toml"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add config"], cwd=project_path, capture_output=True, check=True)

    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "compat fallback", agent="dual", max_retries=2)
    captured = {}

    def _forbid_worktree(*args, **kwargs):
        raise AssertionError("per_task_branch=false should not prepare or merge worktrees")

    def _fake_executor(*args, **kwargs):
        execution_path = Path(kwargs["execution_path"]).resolve()
        captured["execution_path"] = execution_path
        (execution_path / "compat.txt").write_text("main workspace\n", encoding="utf-8")
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_git_prepare_task_worktree", _forbid_worktree)
    monkeypatch.setattr(run_cmd, "_git_merge_task_worktree", _forbid_worktree)
    monkeypatch.setattr(run_cmd, "_run_builtin_executor", _fake_executor)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert current["branch_name"] == "topic"
    assert current["worktree_path"] == str(project_path.resolve())
    assert captured["execution_path"] == project_path.resolve()
    assert (project_path / "compat.txt").read_text(encoding="utf-8") == "main workspace\n"
    assert run_cmd._git_list_worktrees(project_path) == [project_path.resolve()]

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == "topic"


def _git_init_repo(project_path: Path) -> str:
    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)
    return run_cmd._git_current_branch(project_path)


def test_finalize_failed_task_workspace_removes_worktree_branch_and_returns_to_base(tmp_path):
    """Failure path must leave repo in autonomous-loop friendly state.

    Without this helper a review-fail run leaves the task branch + worktree
    behind, the next ``run`` blocks on "uncommitted changes" preflight, and
    the user has to reset things by hand. The helper deletes both and pulls
    the main repo back to base_branch.
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"
    base_branch = _git_init_repo(project_path)

    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected = run_cmd._task_worktree_path(project_info, task_id=42, title="Cleanup demo")
    branch, worktree = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=42,
        title="Cleanup demo",
        base_branch=base_branch,
        worktree_path=expected,
    )

    # Sanity: prepared worktree+branch are alive before cleanup.
    assert worktree.exists()
    assert run_cmd._git_local_branch_exists(project_path, branch)

    run_cmd._finalize_failed_task_workspace(
        task_id=42,
        project_path=project_path,
        worktree_path=worktree,
        task_branch=branch,
        base_branch=base_branch,
    )

    assert worktree.exists() is False
    assert run_cmd._git_worktree_exists(project_path, worktree) is False
    assert run_cmd._git_local_branch_exists(project_path, branch) is False
    assert run_cmd._git_current_branch(project_path) == base_branch


def test_finalize_failed_task_workspace_swallows_cleanup_errors(tmp_path):
    """A worktree that already disappeared on disk must not crash cleanup.

    Autonomous loops cannot afford to bail out on cleanup hiccups — the
    helper logs a warning and moves on, otherwise a transient git error
    would freeze the entire run pipeline.
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    base_branch = _git_init_repo(project_path)

    # Pass a worktree path that was never registered with git; the helper
    # must complete without raising and leave the main repo untouched.
    bogus = tmp_path / "ghost"
    run_cmd._finalize_failed_task_workspace(
        task_id=99,
        project_path=project_path,
        worktree_path=bogus,
        task_branch="feat/task-99-ghost",
        base_branch=base_branch,
    )

    assert run_cmd._git_current_branch(project_path) == base_branch


def test_finalize_failed_task_workspace_cleans_branch_in_single_worktree_mode(tmp_path):
    """Branch 模式下（task_workspace == project_path）任务分支必须自动清理。

    codex review 指出原实现仅在 worktree_path != project_path 时清分支，
    branch 模式失败后会留下孤儿分支，下一轮 ``_git_prepare_task_branch``
    会因 "branch already exists" 直接卡住。本测试守住"自愈语义对所有
    workspace 模式都成立"这个不变量。
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    base_branch = _git_init_repo(project_path)

    branch = run_cmd._git_prepare_task_branch(
        project_path,
        task_id=77,
        title="branch mode failure",
        base_branch=base_branch,
    )
    assert branch
    assert run_cmd._git_local_branch_exists(project_path, branch)
    assert run_cmd._git_current_branch(project_path) == branch

    run_cmd._finalize_failed_task_workspace(
        task_id=77,
        project_path=project_path,
        worktree_path=project_path,  # branch mode: 同一棵 worktree
        task_branch=branch,
        base_branch=base_branch,
    )

    assert run_cmd._git_current_branch(project_path) == base_branch
    assert run_cmd._git_local_branch_exists(project_path, branch) is False, (
        "branch mode failure must auto-delete the task branch"
    )


def test_finalize_failed_task_workspace_keeps_branch_when_worktree_dirty(tmp_path):
    """工作区脏（builder 半提交）时不要切 base、不要丢分支。

    避免静默吞掉用户没 commit 的修改。
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    base_branch = _git_init_repo(project_path)

    branch = run_cmd._git_prepare_task_branch(
        project_path,
        task_id=88,
        title="dirty worktree",
        base_branch=base_branch,
    )
    assert branch
    # Simulate builder leaving uncommitted changes inside the task branch.
    (project_path / "scratch.txt").write_text("half-baked\n", encoding="utf-8")

    run_cmd._finalize_failed_task_workspace(
        task_id=88,
        project_path=project_path,
        worktree_path=project_path,
        task_branch=branch,
        base_branch=base_branch,
    )

    # HEAD must NOT have left the dirty branch (would silently drop changes).
    assert run_cmd._git_current_branch(project_path) == branch
    # And the branch must NOT be deleted (would lose history).
    assert run_cmd._git_local_branch_exists(project_path, branch) is True


def test_finalize_failed_task_workspace_skips_branch_delete_when_head_not_on_base(tmp_path):
    """脏工作区跳过 checkout base 后，必须同时跳过 git branch -D。

    没有这条守卫，``git branch -D <current_branch>`` 会被拒，留下一堆
    "branch 删除失败" 的 yellow warning，分支照样残留。codex 第二次
    review 抓到的真实回归。
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    base_branch = _git_init_repo(project_path)

    branch = run_cmd._git_prepare_task_branch(
        project_path,
        task_id=99,
        title="head still on dirty branch",
        base_branch=base_branch,
    )
    assert branch
    # builder 半提交：工作区脏
    (project_path / "scratch.txt").write_text("half-baked\n", encoding="utf-8")

    # 给 _run_command 加 spy，确认从未被调来执行 git branch -D 当前分支
    original_run = run_cmd._run_command
    branch_delete_attempts: list[list[str]] = []

    def _tracking_run(cmd, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[:2] == ["git", "branch"] and "-D" in cmd:
            branch_delete_attempts.append(list(cmd))
        return original_run(cmd, **kwargs)

    import codepilot.commands.run as run_module
    original_attr = run_module._run_command
    run_module._run_command = _tracking_run
    try:
        run_cmd._finalize_failed_task_workspace(
            task_id=99,
            project_path=project_path,
            worktree_path=project_path,
            task_branch=branch,
            base_branch=base_branch,
        )
    finally:
        run_module._run_command = original_attr

    # HEAD 仍在脏分支（守卫生效）
    assert run_cmd._git_current_branch(project_path) == branch
    # 分支必须仍在（没尝试删 = 没失败）
    assert run_cmd._git_local_branch_exists(project_path, branch) is True
    # 关键断言：从未对当前分支执行 git branch -D（不是"试了但失败了"，是"从一开始就跳过"）
    assert branch_delete_attempts == [], (
        f"expected zero branch-delete attempts when HEAD stays on task branch, got {branch_delete_attempts}"
    )


def test_finalize_failed_task_workspace_abandons_dirty_branch_on_terminal_failure(tmp_path):
    """终态失败允许丢弃 branch 模式下的半成品并回到 base。"""
    project_path = tmp_path / "project"
    project_path.mkdir()
    base_branch = _git_init_repo(project_path)

    branch = run_cmd._git_prepare_task_branch(
        project_path,
        task_id=123,
        title="terminal dirty branch",
        base_branch=base_branch,
    )
    assert branch
    (project_path / "scratch.txt").write_text("discard me\n", encoding="utf-8")

    run_cmd._finalize_failed_task_workspace(
        task_id=123,
        project_path=project_path,
        worktree_path=project_path,
        task_branch=branch,
        base_branch=base_branch,
        abandon_dirty_branch=True,
    )

    assert run_cmd._git_current_branch(project_path) == base_branch
    assert run_cmd._git_local_branch_exists(project_path, branch) is False
    assert (project_path / "scratch.txt").exists() is False
