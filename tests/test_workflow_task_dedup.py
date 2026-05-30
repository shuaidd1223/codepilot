from __future__ import annotations



from codepilot.storage import database as db
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


def test_materialize_inspection_skips_duplicate_fingerprint(tmp_path, monkeypatch):
    """Same fingerprint in existing open inspector task prevents re-creation."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    from codepilot.commands.inspect import _build_content, _materialize_inspection_output
    from codepilot.commands.inspect_signals import lint_fingerprint

    fp = lint_fingerprint("F401")
    content = _build_content(
        {"title": "fix F401", "goal": "\u6e05\u7406", "priority": "P3",
         "kind": "refactor", "evidence": "foo.py F401", "effort": "small",
         "rationale": ""},
        fingerprint=fp,
    )
    db.create_task("demo", "fix F401", content=content,
                   source="inspector", dedup_key="first-key", priority="P3",
                   project_path=str(project_path))

    created, skipped = _materialize_inspection_output(
        [{"title": "fix F401 again", "goal": "\u6e05\u7406 F401", "priority": "P3",
          "kind": "refactor", "evidence": "bar.py F401", "effort": "small"}],
        max_new_tasks=5,
        project_name="demo",
        project_path=project_path,
        priority="P3",
        agent="codex",
        dry_run=False,
    )
    assert len(created) == 0
    assert any("duplicate_fingerprint" in s.get("reason", "") for s in skipped)
