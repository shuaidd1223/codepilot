"""Secrets isolation: provider api_key migration to .codepilot.secrets.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot import config as config_mod
from codepilot.config import (
    SECRETS_FILENAME,
    SECRETS_PATH_ENV,
    load_config,
    sanitize_config_for_display,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv(SECRETS_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return tmp_path


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_sibling_secrets_overlay_supplies_api_key(_isolate):
    project = _isolate / "proj"
    _write(
        project / "AGENTS.toml",
        '[providers.openai-gpt4o]\nmodel = "gpt-4o"\n',
    )
    _write(
        project / SECRETS_FILENAME,
        '[providers.openai-gpt4o]\napi_key = "sk-from-secrets"\n',
    )

    cfg = load_config(project / "AGENTS.toml")
    assert cfg is not None
    assert cfg.providers["openai-gpt4o"].api_key == "sk-from-secrets"
    # Non-secret fields preserved from AGENTS.toml.
    assert cfg.providers["openai-gpt4o"].model == "gpt-4o"


def test_env_secrets_path_overrides_sibling(_isolate, monkeypatch):
    project = _isolate / "proj"
    elsewhere = _isolate / "elsewhere" / "secrets.toml"

    _write(project / "AGENTS.toml", '[providers.openai-gpt4o]\nmodel = "gpt-4o"\n')
    _write(project / SECRETS_FILENAME, '[providers.openai-gpt4o]\napi_key = "sk-sibling"\n')
    _write(elsewhere, '[providers.openai-gpt4o]\napi_key = "sk-env"\n')

    monkeypatch.setenv(SECRETS_PATH_ENV, str(elsewhere))

    cfg = load_config(project / "AGENTS.toml")
    assert cfg is not None
    assert cfg.providers["openai-gpt4o"].api_key == "sk-env"


def test_inline_api_key_in_agents_toml_logs_warning(_isolate, monkeypatch, caplog):
    """Existing installs that still hold api_key in AGENTS.toml keep working
    but receive a single warning telling them to migrate."""
    import logging

    project = _isolate / "proj"
    _write(
        project / "AGENTS.toml",
        '[providers.openai-gpt4o]\napi_key = "sk-inline"\n',
    )

    # codepilot keeps propagate=False to avoid duplicate console output;
    # logger.py mirrors pytest's capture handler so caplog still sees it.
    from codepilot import logger as logger_mod

    logger_mod.reset_for_tests()
    logger = logger_mod.get_logger("config")
    caplog.set_level(logging.WARNING, logger=logger.name)

    cfg = load_config(project / "AGENTS.toml")
    assert cfg is not None
    assert cfg.providers["openai-gpt4o"].api_key == "sk-inline"

    messages = [rec.message for rec in caplog.records if "AGENTS.toml" in rec.message]
    assert messages, f"expected migration warning, got: {[r.message for r in caplog.records]}"
    assert "openai-gpt4o" in messages[0]


def test_secrets_only_contributes_api_key(_isolate):
    """A secrets file shouldn't be able to override model / endpoint —
    that would defeat the security boundary."""
    project = _isolate / "proj"
    _write(project / "AGENTS.toml", '[providers.openai-gpt4o]\nmodel = "gpt-4o"\n')
    _write(
        project / SECRETS_FILENAME,
        '[providers.openai-gpt4o]\napi_key = "sk"\nmodel = "MALICIOUS"\nbase_url = "http://attacker"\n',
    )

    cfg = load_config(project / "AGENTS.toml")
    assert cfg is not None
    assert cfg.providers["openai-gpt4o"].api_key == "sk"
    assert cfg.providers["openai-gpt4o"].model == "gpt-4o"
    assert cfg.providers["openai-gpt4o"].base_url == ""


def test_sanitize_config_scrubs_api_key(_isolate):
    project = _isolate / "proj"
    _write(project / "AGENTS.toml", '[providers.openai-gpt4o]\napi_key = "sk-secret"\n')

    cfg = load_config(project / "AGENTS.toml")
    assert cfg is not None
    safe = sanitize_config_for_display(cfg)

    assert safe.providers["openai-gpt4o"].api_key == "***"
    # Original untouched.
    assert cfg.providers["openai-gpt4o"].api_key == "sk-secret"
