from __future__ import annotations

from pathlib import Path

import tomllib


def test_dev_extra_includes_project_level_ruff():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert any(dep.lower().startswith("ruff") for dep in dev_dependencies)
