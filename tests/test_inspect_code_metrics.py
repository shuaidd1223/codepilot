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
    assert "生产代码：1 个文件" in result
    assert "测试代码：0 个文件" in result
    assert "Python: 1 文件" in result
    assert "app.py" in result
    assert "tangled" in result
    assert "生产复杂函数" in result
    assert "node_modules" not in result


def test_collect_code_metrics_separates_test_hotspots(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    tests_dir = project / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_big.py").write_text(
        "\n".join(
            [
                "def test_tangled(a, b, c):",
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

    result = collect_code_metrics(project)

    assert "生产代码：0 个文件" in result
    assert "测试代码：1 个文件" in result
    assert "测试文件（仅参考，不单独触发任务）" in result
    assert "测试复杂函数（仅参考，不单独触发任务）" in result
    assert "tests/test_big.py" in result
    assert "生产复杂函数" not in result


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
    assert "生产复杂函数" in captured["prompt"]
    assert "app.py:1 tangled" in captured["prompt"]
    assert "## 信号 1：最近 git 提交\n（跳过）" in captured["prompt"]


def test_filter_candidates_drops_code_metrics_only_test_refactor(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    tests_dir = project / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_big.py").write_text("def test_tangled():\n    assert True\n", encoding="utf-8")
    signal_results = [
        inspect_cmd.InspectSignalResult(
            key="code_metrics",
            title="代码规模与复杂度线索",
            order=7,
            enabled=True,
            content=(
                "测试文件（仅参考，不单独触发任务）:\n"
                "- tests/test_big.py: 30 行，分支复杂度约 21"
            ),
        )
    ]

    kept, dropped = inspect_cmd._filter_candidates(
        [
            {
                "title": "收口测试复杂度",
                "goal": "拆分 tests/test_big.py 里的复杂测试函数。",
                "priority": "P4",
                "rationale": "测试复杂度较高。",
                "kind": "refactor",
                "evidence": "signal 7: tests/test_big.py:1 分支复杂度约 21",
                "effort": "small",
            }
        ],
        signal_results=signal_results,
        project_path=project,
    )

    assert kept == []
    assert dropped == [{"title": "收口测试复杂度", "reason": "test_file_metric_only"}]
