"""Structured logger for operational events.

``codepilot.output`` owns *user-facing* terminal output — Rich markup,
TTY colours, progress spinners. That's a rendering concern and stays
where it is.

This module owns *operational* logging — the kind of records you want
to grep at 2am to figure out why a planner call silently fell back to
a default. Key properties:

* A single ``logging.Logger`` (named ``codepilot``) so everything in
  the codebase writes through one rotating file handler.
* Log file defaults to ``~/.codepilot/logs/codepilot.log`` with a
  10-file / 1 MB rotation — safe for long-running daemons without
  manual log hygiene.
* Console output is stderr-only so it doesn't interleave with the
  command-line JSON payloads produced by ``codepilot --json``.
* Level defaults to ``INFO``; override via ``CODEPILOT_LOG_LEVEL``
  (case-insensitive).

Existing callers that do ``sys.stderr.write(f"  [planner] ...")`` can
migrate incrementally to :func:`get_logger(...).info(...)`. The legacy
direct-stderr writes keep working unchanged.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_LOG_DIR = Path.home() / ".codepilot" / "logs"
_LOG_FILE = _LOG_DIR / "codepilot.log"
_LOGGER_NAME = "codepilot"
_CONFIGURED = False
_PYTEST_CAPTURE_HANDLER = ("_pytest.logging", "LogCaptureHandler")


def _resolve_log_level() -> int:
    raw = os.environ.get("CODEPILOT_LOG_LEVEL", "INFO").strip().upper()
    return logging.getLevelName(raw) if raw else logging.INFO


def _ensure_configured() -> logging.Logger:
    """Idempotently attach our handlers to the ``codepilot`` logger.

    We attach handlers exactly once even if :func:`get_logger` is called
    from many modules — repeat calls return the same configured logger.
    """
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)
    if _CONFIGURED:
        _sync_pytest_capture_handler(logger)
        return logger

    logger.setLevel(_resolve_log_level())
    logger.propagate = False  # don't duplicate into root logger; pytest capture is bridged explicitly

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — best-effort: if we can't create the log
    # directory (read-only FS, sandboxed test env), we silently skip it
    # and rely on the stderr handler alone.
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=1_000_000,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass

    # Stderr so it doesn't pollute stdout (important for --json commands).
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    # Console mirrors at WARNING to avoid spamming the terminal with INFO;
    # the file handler keeps the full trail.
    stream_handler.setLevel(logging.WARNING)
    logger.addHandler(stream_handler)

    _CONFIGURED = True
    _sync_pytest_capture_handler(logger)
    return logger


def _is_pytest_capture_handler(handler: logging.Handler) -> bool:
    handler_type = handler.__class__
    return (
        handler_type.__module__,
        handler_type.__name__,
    ) == _PYTEST_CAPTURE_HANDLER


def _sync_pytest_capture_handler(logger: logging.Logger) -> None:
    """Mirror pytest's caplog handler while keeping ``propagate`` disabled.

    ``caplog`` listens through a ``LogCaptureHandler`` attached to the
    root logger. Since ``codepilot`` deliberately keeps ``propagate``
    disabled to avoid duplicate console output, pytest would otherwise
    miss our records. When pytest's capture handler is present, attach
    that exact handler object to the base logger as well.
    """
    root_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if _is_pytest_capture_handler(handler)
    ]
    attached_handlers = [
        handler for handler in logger.handlers if _is_pytest_capture_handler(handler)
    ]

    for handler in attached_handlers:
        if handler not in root_handlers:
            logger.removeHandler(handler)

    for handler in root_handlers:
        if handler not in logger.handlers:
            logger.addHandler(handler)


def get_logger(suffix: Optional[str] = None) -> logging.Logger:
    """Return the shared ``codepilot`` logger, optionally scoped by *suffix*.

    A suffix like ``planner`` or ``webui`` becomes ``codepilot.planner``
    in log lines, which makes grepping by subsystem trivial.
    """
    _ensure_configured()
    if suffix:
        return logging.getLogger(f"{_LOGGER_NAME}.{suffix}")
    return logging.getLogger(_LOGGER_NAME)


def reset_for_tests() -> None:
    """Strip handlers so tests don't write the real log file.

    Only touch this from within ``tests/``; production code never needs
    to reconfigure the logger.
    """
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    _CONFIGURED = False
