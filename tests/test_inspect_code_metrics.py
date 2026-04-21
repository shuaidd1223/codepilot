from __future__ import annotations

from types import SimpleNamespace

from codepilot.commands import inspect as inspect_cmd
from codepilot.commands.inspect import collect_code_metrics


def test_collect_code_metrics_reports_size_and_python_complexity(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "app.py").write_text(
        "\n".join(
            [
                "def tiny():",
                "    return 1",
                "",
                "def tangled(a, b, c):",
                "    if a:",
                "        pass",
                "    if b:",
                "        pass",
                "    if c:",
                "        pass",
                "    for x in range(3):",
                "        if x and a or b:",
                "            pass",
                "    while c:",
                "        break",
                "    try:",
                "        pass",
                "    except ValueError:",
                "        pass",
            ]
        ),
        encoding="utf-8",
    )
    vendor = project / "node_modules"
    vendor.mkdir()
    (vendor / "ignored.js").write_text("if (x) {}\n", encoding="utf-8")

    result = collect_code_metrics(project)

    assert "总体：1 个代码文件" in result
    assert "Python: 1 文件" in result
    assert "app.py" in result
    assert "tangled" in result
    assert "复杂函数" in result
    assert "node_modules" not in result


def test_collect_code_metrics_returns_empty_message_without_code(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "README.md").write_text("# demo\n", encoding="utf-8")

    assert collect_code_metrics(project) == "（未发现可扫描的代码文件）"


def test_run_inspection_includes_complexity_signal_in_prompt(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "app.py").write_text(
        "\n".join(
            [
                "def tangled(a, b, c):",
                "    if a:",
                "        pass",
                "    if b:",
                "        pass",
                "    if c:",
                "        pass",
                "    for x in range(3):",
                "        if x and a or b:",
                "            pass",
                "    while c:",
                "        break",
                "    try:",
                "        pass",
                "    except ValueError:",
                "        pass",
            ]
        ),
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
        signals=("complexity",),
        dry_run=True,
    )

    assert result["candidates_total"] == 0
    assert "## 信号 7：代码规模与复杂度线索" in captured["prompt"]
    assert "总体：1 个代码文件" in captured["prompt"]
    assert "复杂函数" in captured["prompt"]
    assert "app.py:1 tangled" in captured["prompt"]
    assert "## 信号 1：最近 git 提交\n（跳过）" in captured["prompt"]
