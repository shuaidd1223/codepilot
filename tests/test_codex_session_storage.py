from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codepilot.codex.session_storage import (
    codex_session_exists,
    latest_codex_session_id,
)


def _write_session(
    root: Path,
    *,
    date: tuple[str, str, str],
    session_id: str,
    cwd: str,
    timestamp: str = "2026-05-11T16-18-24",
    mtime: float | None = None,
) -> Path:
    year, month, day = date
    target = root / year / month / day
    target.mkdir(parents=True, exist_ok=True)
    file = target / f"rollout-{timestamp}-{session_id}.jsonl"
    payload = {
        "timestamp": "2026-05-11T08:19:06.634Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "timestamp": "2026-05-11T08:18:24.622Z",
            "cwd": cwd,
            "originator": "test",
        },
    }
    file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(file, (mtime, mtime))
    return file


def test_codex_session_exists_matches_id_and_cwd(tmp_path: Path):
    root = tmp_path / "sessions"
    project = tmp_path / "proj"
    project.mkdir()
    _write_session(
        root,
        date=("2026", "05", "11"),
        session_id="abc-1",
        cwd=str(project),
    )
    assert codex_session_exists(project, "abc-1", sessions_root=root) is True


def test_codex_session_exists_returns_false_for_different_cwd(tmp_path: Path):
    root = tmp_path / "sessions"
    project = tmp_path / "proj"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    _write_session(
        root,
        date=("2026", "05", "11"),
        session_id="abc-1",
        cwd=str(other),
    )
    assert codex_session_exists(project, "abc-1", sessions_root=root) is False


def test_codex_session_exists_returns_false_when_no_file(tmp_path: Path):
    root = tmp_path / "sessions"
    project = tmp_path / "proj"
    project.mkdir()
    root.mkdir()
    assert codex_session_exists(project, "missing", sessions_root=root) is False


def test_codex_session_exists_returns_false_when_root_missing(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    assert codex_session_exists(project, "x", sessions_root=tmp_path / "absent") is False


def test_codex_session_exists_rejects_empty_session_id(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    assert codex_session_exists(project, "", sessions_root=tmp_path / "sessions") is False


def test_latest_codex_session_id_returns_most_recent_for_project(tmp_path: Path):
    root = tmp_path / "sessions"
    project = tmp_path / "proj"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _write_session(
        root,
        date=("2026", "05", "10"),
        session_id="older",
        cwd=str(project),
        mtime=1700000000,
    )
    _write_session(
        root,
        date=("2026", "05", "11"),
        session_id="newer",
        cwd=str(project),
        mtime=1700000100,
    )
    _write_session(
        root,
        date=("2026", "05", "11"),
        session_id="unrelated",
        cwd=str(other),
        mtime=1700000200,
    )
    assert latest_codex_session_id(project, sessions_root=root) == "newer"


def test_latest_codex_session_id_returns_empty_when_no_match(tmp_path: Path):
    root = tmp_path / "sessions"
    project = tmp_path / "proj"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _write_session(
        root,
        date=("2026", "05", "11"),
        session_id="ses",
        cwd=str(other),
    )
    assert latest_codex_session_id(project, sessions_root=root) == ""


def test_latest_codex_session_id_returns_empty_when_root_missing(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    assert latest_codex_session_id(project, sessions_root=tmp_path / "missing") == ""


def test_latest_codex_session_id_skips_invalid_jsonl(tmp_path: Path):
    root = tmp_path / "sessions"
    project = tmp_path / "proj"
    project.mkdir()
    bad = root / "2026" / "05" / "11"
    bad.mkdir(parents=True)
    (bad / "rollout-2026-05-11T01-00-00-broken.jsonl").write_text("not json\n", encoding="utf-8")
    _write_session(
        root,
        date=("2026", "05", "11"),
        session_id="good",
        cwd=str(project),
    )
    assert latest_codex_session_id(project, sessions_root=root) == "good"
