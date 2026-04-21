"""Structured logger wiring."""

from __future__ import annotations

import logging

from codepilot import logger as logger_mod


def teardown_function(_):
    logger_mod.reset_for_tests()


def test_get_logger_is_configured_once(monkeypatch, tmp_path):
    monkeypatch.setattr(logger_mod, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logger_mod, "_LOG_FILE", tmp_path / "logs" / "codepilot.log")

    first = logger_mod.get_logger()
    second = logger_mod.get_logger()
    assert first is second
    # Each call mustn't stack a new set of handlers.
    assert len([h for h in first.handlers]) >= 1
    handler_count = len(first.handlers)
    logger_mod.get_logger()
    assert len(first.handlers) == handler_count


def test_get_logger_scopes_by_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(logger_mod, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logger_mod, "_LOG_FILE", tmp_path / "logs" / "codepilot.log")

    base = logger_mod.get_logger()
    scoped = logger_mod.get_logger("planner")
    assert scoped.name == "codepilot.planner"
    # Scoped loggers inherit handlers from the parent, so we don't double-attach.
    assert scoped.parent is base


def test_log_respects_env_level(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEPILOT_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(logger_mod, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logger_mod, "_LOG_FILE", tmp_path / "logs" / "codepilot.log")

    logger = logger_mod.get_logger()
    assert logger.level == logging.DEBUG


def test_default_log_file_uses_project_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.toml").write_text(
        """
[project]
name = "demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    logger = logger_mod.get_logger("test")
    logger.warning("project log route")
    for handler in logger_mod.get_logger().handlers:
        handler.flush()

    log_file = tmp_path / "home" / ".codepilot" / "data" / "demo" / "logs" / "codepilot.log"
    assert "project log route" in log_file.read_text(encoding="utf-8")


def test_caplog_captures_warning_without_enabling_propagation(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(logger_mod, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logger_mod, "_LOG_FILE", tmp_path / "logs" / "codepilot.log")

    logger = logger_mod.get_logger("config")
    caplog.set_level(logging.WARNING, logger=logger.name)

    logger.warning("inline secret warning")

    assert [rec.getMessage() for rec in caplog.records] == ["inline secret warning"]
    assert logger_mod.get_logger().propagate is False
