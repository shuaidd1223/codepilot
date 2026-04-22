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
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "this.cp.toggleCategory(project, cat);" in sidebar
    assert "const CATEGORY_VIEWS = ['sessions', 'tasks', 'jobs'];" in app_state
    assert "function _openProjectCategory(project, view)" in app_state


def test_web_ui_bootstrap_wires_app_state_boundary_before_mount():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/boundaries/AppStateBoundary.js\"></script>" in index_html
    assert "setup: CP.AppStateBoundary.setup," in app


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


def test_web_ui_wires_plugin_diff_assets():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    diff_viewer = Path("codepilot/web/components/DiffViewer.js").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "diff2html/bundles/css/diff2html.min.css" in index_html
    assert "diff2html/bundles/js/diff2html.min.js" in index_html
    assert "<script src=\"/static/components/DiffViewer.js\"></script>" in index_html
    assert "CP.Components.DiffViewer" in diff_viewer
    assert "window.Diff2Html.html" in diff_viewer
    assert "CP.ensureMonaco" not in utils


def test_web_ui_action_protocol_exposes_scoped_pending_keys():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "const ACTION_KEYS = Object.freeze({" in app_state
    assert "GOAL_SUBMIT: 'goal.submit'" in app_state
    assert "COMPOSER_SUBMIT: 'composer.submit'" in app_state
    assert "SESSION_SEND: 'session.send'" in app_state
    assert "SESSION_DELETE: 'session.delete'" in app_state
    assert "SESSION_CLARIFY_REPLY: 'session.clarify.reply'" in app_state
    assert "isActionPending: (actionKey) => _isActionPending(actionKey)" in app_state
    assert "ACTION_KEYS," in app_state


def test_form_components_use_scoped_action_pending_instead_of_global_sending():
    goal = Path("codepilot/web/components/GoalInput.js").read_text(encoding="utf-8")
    composer = Path("codepilot/web/components/Composer.js").read_text(encoding="utf-8")
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")

    assert "goalPending()" in goal
    assert "composerPending()" in composer
    assert "chatPending()" in chat
    assert "clarifyPending()" in chat
    assert "deletePending()" in chat
    assert "s.sending" not in goal
    assert "s.sending" not in composer
    assert "s.sending" not in chat


def test_agent_log_splits_rendering_and_interaction_state_into_boundaries():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    agent_log = Path("codepilot/web/components/AgentLog.js").read_text(encoding="utf-8")
    render_boundary = Path("codepilot/web/boundaries/AgentLogRenderBoundary.js").read_text(encoding="utf-8")
    interaction_boundary = Path("codepilot/web/boundaries/AgentLogInteractionBoundary.js").read_text(encoding="utf-8")
    contract_boundary = Path("codepilot/web/boundaries/AgentLogBoundaryContract.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/boundaries/AgentLogRenderBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AgentLogInteractionBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AgentLogBoundaryContract.js\"></script>" in index_html
    assert "const AgentLogAdapter = CP.AgentLogBoundaryContract.createAdapter({" in agent_log
    assert "const AgentLogRender = AgentLogAdapter.render;" in agent_log
    assert "const AgentLogInteraction = AgentLogAdapter.interaction;" in agent_log
    assert "CP.AgentLogRenderBoundary = CP.AgentLogRenderBoundary || (() => {" in render_boundary
    assert "CP.AgentLogInteractionBoundary = CP.AgentLogInteractionBoundary || (() => {" in interaction_boundary
    assert "CP.AgentLogBoundaryContract = CP.AgentLogBoundaryContract || (() => {" in contract_boundary
    assert "function createAdapter(options = {}) {" in contract_boundary


def test_agent_log_contract_exposes_stable_adapter_surface():
    contract_boundary = Path("codepilot/web/boundaries/AgentLogBoundaryContract.js").read_text(encoding="utf-8")

    for marker in (
        "createMarkdownCache:",
        "renderMarkdown:",
        "parseMarkdownBlocks:",
        "scheduleEnhance:",
        "enhanceCodeBlocks:",
        "findSearchMatches:",
        "searchSummary:",
        "linesLabel:",
        "jumpLabel:",
        "handleTextLengthChanged:",
        "handleLineCountChanged:",
        "handleSearchQueryChanged:",
        "handleSearchMatchesChanged:",
        "toggleFollow:",
        "onSearchKeydown:",
        "nextMatch:",
        "prevMatch:",
        "scrollToBlock:",
        "onScroll:",
        "scrollToBottom:",
    ):
        assert marker in contract_boundary
