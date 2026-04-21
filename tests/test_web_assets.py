from __future__ import annotations

from pathlib import Path


def test_task_detail_component_keeps_single_computed_block():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert source.count("computed:") == 1
    assert "task() { return this.s.taskDetail; }" in source
    assert "canSplit()" in source


def test_task_detail_keeps_single_live_log_panel():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert "实时日志" in source
    assert "实时进度" not in source
    assert "<cp-live-log" not in source


def test_task_detail_distinguishes_loading_and_empty_state():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert 'v-if="s.taskDetailLoading && !task"' in source
    assert "暂无任务详情" in source


def test_sidebar_category_toggle_uses_project_scoped_accordion():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")

    assert "this.cp.toggleCategory(project, cat);" in sidebar
    assert "const CATEGORY_VIEWS = ['sessions', 'tasks', 'jobs'];" in app
    assert "function _openProjectCategory(project, view)" in app


def test_web_ui_uses_in_app_confirm_dialog_instead_of_browser_dialogs():
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")
    task_detail = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")

    assert "<script src=\"/static/components/ConfirmDialog.js\"></script>" in index_html
    assert "<cp-confirm-dialog></cp-confirm-dialog>" in app
    assert "if (!confirm(" not in app
    assert "if (!confirm(" not in task_detail
    assert "window.confirm(" not in app
    assert "window.confirm(" not in task_detail
    assert "window.alert(" not in app
    assert "window.prompt(" not in app
