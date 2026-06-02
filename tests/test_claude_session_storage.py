from __future__ import annotations

import os
from pathlib import Path

from codepilot.claude.session_storage import (
    claude_session_exists,
    encode_claude_project_dir,
    latest_claude_session_id,
)


def test_encode_claude_project_dir_replaces_windows_separators():
    encoded = encode_claude_project_dir(Path(r"D:\myCode\workflow"))
    assert encoded == "D--myCode-workflow"


def test_encode_claude_project_dir_replaces_posix_separators():
    encoded = encode_claude_project_dir(Path("/home/user/proj"))
    assert encoded == "-home-user-proj"


def test_latest_claude_session_id_returns_empty_when_projects_root_missing(tmp_path: Path):
    assert latest_claude_session_id(tmp_path / "project", projects_root=tmp_path / "missing") == ""


def test_latest_claude_session_id_returns_empty_when_project_dir_missing(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    project = tmp_path / "myProject"
    project.mkdir()
    assert latest_claude_session_id(project, projects_root=root) == ""


def test_latest_claude_session_id_returns_latest_jsonl_stem(tmp_path: Path):
    root = tmp_path / "projects"
    project = tmp_path / "myProject"
    project.mkdir()
    encoded = encode_claude_project_dir(project)
    target = root / encoded
    target.mkdir(parents=True)
    older = target / "older.jsonl"
    newer = target / "newer.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    os.utime(older, (1700000000, 1700000000))
    os.utime(newer, (1700000100, 1700000100))
    assert latest_claude_session_id(project, projects_root=root) == "newer"


def test_latest_claude_session_id_ignores_non_jsonl_files(tmp_path: Path):
    root = tmp_path / "projects"
    project = tmp_path / "myProject"
    project.mkdir()
    encoded = encode_claude_project_dir(project)
    target = root / encoded
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("hello", encoding="utf-8")
    (target / "memory").mkdir()
    assert latest_claude_session_id(project, projects_root=root) == ""


def test_claude_session_exists_returns_true_when_jsonl_present(tmp_path: Path):
    root = tmp_path / "projects"
    project = tmp_path / "myProject"
    project.mkdir()
    target = root / encode_claude_project_dir(project)
    target.mkdir(parents=True)
    (target / "ses-xyz.jsonl").write_text("{}\n", encoding="utf-8")
    assert claude_session_exists(project, "ses-xyz", projects_root=root) is True


def test_claude_session_exists_returns_false_when_jsonl_absent(tmp_path: Path):
    root = tmp_path / "projects"
    project = tmp_path / "myProject"
    project.mkdir()
    target = root / encode_claude_project_dir(project)
    target.mkdir(parents=True)
    assert claude_session_exists(project, "ses-xyz", projects_root=root) is False


def test_claude_session_exists_returns_false_when_project_dir_missing(tmp_path: Path):
    assert claude_session_exists(tmp_path / "missing", "ses", projects_root=tmp_path / "projects") is False


def test_claude_session_exists_returns_false_for_empty_session_id(tmp_path: Path):
    root = tmp_path / "projects"
    project = tmp_path / "myProject"
    project.mkdir()
    target = root / encode_claude_project_dir(project)
    target.mkdir(parents=True)
    (target / ".jsonl").write_text("{}\n", encoding="utf-8")
    assert claude_session_exists(project, "", projects_root=root) is False


def test_latest_claude_session_id_skips_subdirectories(tmp_path: Path):
    root = tmp_path / "p"
    project = tmp_path / "proj"
    project.mkdir()
    encoded = encode_claude_project_dir(project)
    target = root / encoded
    if len(str(target)) > 200:
        import pytest
        pytest.skip("Windows path too long for this test")
    target.mkdir(parents=True)
    (target / "abc.jsonl").write_text("{}\n", encoding="utf-8")
    nested = target / "nested.jsonl"
    nested.mkdir()
    assert latest_claude_session_id(project, projects_root=root) == "abc"
