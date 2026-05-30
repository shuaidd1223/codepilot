from __future__ import annotations

from codepilot.ai_support.providers import _collect_project_context


def test_collect_project_context_auto_discovers_nested_project_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "myCode").mkdir()
    project = home / "myCode" / "workflow"
    project.mkdir()
    (project / "AGENTS.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project / "README.md").write_text("# Demo\n这是一个自动化工作流项目。", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    context = _collect_project_context(str(home), query_text="当前项目的功能是什么")

    assert "自动探测" in context
    assert str(project) in context
    assert "自动化工作流项目" in context


def test_collect_project_context_marks_home_like_directory(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for name in ("Desktop", "Documents", "Downloads", "Pictures"):
        (home / name).mkdir()

    context = _collect_project_context(str(home), query_text="当前项目是什么")

    assert "更像用户主目录" in context
    assert "codepilot init" in context


def test_collect_project_context_uses_rich_summary_for_project_root(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "src").mkdir()
    (project / "README.md").write_text("# Repo\n一个示例项目。", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")

    context = _collect_project_context(str(project), query_text="分析项目结构")

    assert "structured overview" in context
    assert "Directory Layout" in context

