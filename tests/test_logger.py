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
