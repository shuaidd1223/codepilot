from __future__ import annotations

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
