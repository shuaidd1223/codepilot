from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from codepilot.core.config import AgentsConfig
from codepilot.opencode.env import build_opencode_config_from_agents_config
from codepilot.opencode.model_state import (
    load_latest_project_model,
    load_latest_project_session_id,
    load_saved_project_model,
    resolve_project_model_selection,
    save_project_model_selection,
    sync_latest_project_model_selection,
)
from codepilot.opencode.paths import opencode_runtime_db_path
from codepilot.opencode.profile import build_opencode_profile
from codepilot.storage import database as db


@pytest.fixture(autouse=True)
def _isolate_codepilot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))


def test_load_latest_project_model_reads_opencode_session_db(tmp_path: Path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    other_path = tmp_path / "other"
    other_path.mkdir()
    opencode_db = tmp_path / "opencode.db"
    _write_opencode_session_db(
        opencode_db,
        [
            (project_path, {"providerID": "deepseek", "id": "deepseek-v4-flash", "variant": "high"}, 20),
            (project_path, {"providerID": "openai", "id": "gpt-5.4"}, 10),
            (other_path, {"providerID": "opencode", "id": "minimax-m2.5-free"}, 30),
        ],
    )

    assert load_latest_project_model(project_path, db_path=opencode_db) == {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-flash",
        "variant": "high",
    }


def test_load_latest_project_session_id_reads_scoped_opencode_session_db(tmp_path: Path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    other_path = tmp_path / "other"
    other_path.mkdir()
    opencode_db = tmp_path / "opencode.db"
    _write_opencode_session_db(
        opencode_db,
        [
            (project_path, {"providerID": "openai", "id": "gpt-5.4"}, 10),
            (project_path, {"providerID": "deepseek", "id": "deepseek-v4-pro"}, 20),
            (other_path, {"providerID": "opencode", "id": "minimax-m2.5-free"}, 30),
        ],
    )

    assert load_latest_project_session_id(project_path, db_path=opencode_db) == "ses_1"


def test_load_latest_project_model_without_scoped_db_does_not_read_native_opencode_state(tmp_path: Path):
    project_path = tmp_path / "project"
    project_path.mkdir()

    assert load_latest_project_model(project_path) == {}


def test_opencode_runtime_db_path_is_codepilot_scoped(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("codepilot.opencode.paths.global_storage_root", lambda: tmp_path / "codepilot-home")

    assert opencode_runtime_db_path("demo") == (
        tmp_path / "codepilot-home" / "opencode" / "demo" / "xdg-data" / "opencode" / "opencode.db"
    )


def test_latest_project_model_overrides_generated_default(tmp_path: Path):
    cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": "sk-openai",
                    "base_url": "https://proxy.example/v1",
                    "model": "gpt-5.4",
                }
            }
        }
    )

    opencode_cfg = build_opencode_config_from_agents_config(
        cfg,
        preferred_model={"provider_id": "opencode", "model_id": "minimax-m2.5-free"},
    )
    profile = build_opencode_profile(opencode_cfg, mcp_servers={}, base_path=tmp_path / "runtime")
    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])

    assert payload["model"] == "opencode/minimax-m2.5-free"
    assert payload["agent"]["codepilot"]["model"] == "opencode/minimax-m2.5-free"
    assert payload["provider"]["openai"]["models"]["gpt-5.4"]["name"] == "GPT-5.4"


def test_saved_project_model_overrides_latest_default_session(tmp_path: Path):
    db.init_db()
    project_path = tmp_path / "project"
    project_path.mkdir()
    opencode_db = tmp_path / "opencode.db"
    _write_opencode_session_db(
        opencode_db,
        [
            (project_path, {"providerID": "opencode", "id": "minimax-m2.5-free"}, 30),
        ],
    )

    save_project_model_selection("demo", {"provider_id": "deepseek", "model_id": "deepseek-v4-pro"})

    assert load_saved_project_model("demo") == {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
    }
    assert resolve_project_model_selection("demo", project_path, db_path=opencode_db) == {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
    }


def test_unpersisted_builtin_default_session_does_not_override_project_config(tmp_path: Path):
    db.init_db()
    project_path = tmp_path / "project"
    project_path.mkdir()
    opencode_db = tmp_path / "opencode.db"
    _write_opencode_session_db(
        opencode_db,
        [
            (project_path, {"providerID": "opencode", "id": "minimax-m2.5-free"}, 30),
        ],
    )

    assert resolve_project_model_selection("demo", project_path, db_path=opencode_db) == {}


def test_sync_latest_project_model_persists_project_selection(tmp_path: Path):
    db.init_db()
    project_path = tmp_path / "project"
    project_path.mkdir()
    opencode_db = tmp_path / "opencode.db"
    _write_opencode_session_db(
        opencode_db,
        [
            (project_path, {"providerID": "openai", "id": "gpt-5.4"}, 10),
            (project_path, {"providerID": "deepseek", "id": "deepseek-v4-pro", "variant": "high"}, 20),
        ],
    )

    assert sync_latest_project_model_selection("demo", project_path, db_path=opencode_db) == {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "variant": "high",
    }
    assert load_saved_project_model("demo") == {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "variant": "high",
    }


def _write_opencode_session_db(
    path: Path,
    rows: list[tuple[Path, dict[str, str], int]],
) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        create table session (
            id text primary key,
            directory text not null,
            model text,
            time_updated integer not null
        )
        """
    )
    for index, (directory, model, updated) in enumerate(rows):
        con.execute(
            "insert into session (id, directory, model, time_updated) values (?, ?, ?, ?)",
            (f"ses_{index}", str(directory.resolve()), json.dumps(model), updated),
        )
    con.commit()
    con.close()
