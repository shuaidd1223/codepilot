from __future__ import annotations

import os
import time
from types import SimpleNamespace

from codepilot.commands import inspect as inspect_cmd
from codepilot.commands.inspect import collect_dependency_health


def test_collect_dependency_health_flags_package_without_lock(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "package.json").write_text(
        '{"dependencies": {"left-pad": "^1.3.0"}}',
        encoding="utf-8",
    )

    result = collect_dependency_health(project)

    assert "package.json" in result
    assert "缺少 lockfile" in result


def test_collect_dependency_health_flags_stale_package_lock(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    package_json = project / "package.json"
    package_lock = project / "package-lock.json"
    package_lock.write_text("{}", encoding="utf-8")
    package_json.write_text('{"dependencies": {"left-pad": "^1.3.0"}}', encoding="utf-8")

    old = time.time() - 120
    os.utime(package_lock, (old, old))
    now = time.time()
    os.utime(package_json, (now, now))

    result = collect_dependency_health(project)

    assert "package.json 比 lockfile 更新" in result


def test_collect_dependency_health_flags_unpinned_requirements(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text(
        "\n".join(
            [
                "click==8.1.7",
                "rich",
                "# comment",
                "--extra-index-url https://example.invalid/simple",
            ]
        ),
        encoding="utf-8",
    )

    result = collect_dependency_health(project)

    assert "requirements.txt" in result
    assert "1 个依赖未固定版本" in result


def test_collect_dependency_health_is_quiet_without_findings(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    assert collect_dependency_health(project) == "（无明显依赖健康问题；未联网检查最新版本）"


def test_run_inspection_includes_dependency_health_signal_in_prompt(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "package.json").write_text(
        '{"dependencies": {"left-pad": "^1.3.0"}}',
        encoding="utf-8",
    )
    captured = {}

    def fake_call_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"candidates": []}

    monkeypatch.setattr(inspect_cmd, "_call_llm", fake_call_llm)
    monkeypatch.setattr(inspect_cmd, "_existing_titles", lambda project_name: "（无）")
    monkeypatch.setattr(inspect_cmd.db, "existing_dedup_keys", lambda project_name: set())
    monkeypatch.setattr(
        inspect_cmd,
        "load_project_config",
        lambda project_info: SimpleNamespace(
            classifier=None,
            providers={},
            get_provider_api_key=lambda provider_key: None,
        ),
    )

    result = inspect_cmd.run_inspection(
        {"name": "demo", "path": str(project)},
        signals=("deps",),
        dry_run=True,
    )

    assert result["candidates_total"] == 0
    assert "## 信号 6：依赖健康线索" in captured["prompt"]
    assert "package.json: 发现依赖但缺少 lockfile" in captured["prompt"]
    assert "## 信号 1：最近 git 提交\n（跳过）" in captured["prompt"]
    assert "## 信号 7：代码规模与复杂度线索\n（跳过）" in captured["prompt"]
