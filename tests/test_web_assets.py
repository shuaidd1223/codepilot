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
    assert "taskContentText()" in source
    assert "任务正文为空" in source


def test_task_detail_renders_structured_reviewer_verdict():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    # Computed helpers that hydrate the verdict panel from either the
    # top-level latest_review payload or the per-log review blocks.
    assert "latestReview()" in source
    assert "verdictToneClass()" in source
    assert "t.latest_review" in source

    # Render blocks: badge, AC table, blockers, advisory.
    assert '"block reviewer-verdict"' in source
    assert "最新审查结论" in source
    assert "reviewer-verdict-badge" in source
    assert "ac-checks-table" in source
    assert "阻塞点" in source
    assert "非阻塞观察" in source

    # CSS must carry the tone classes the template applies.
    assert ".reviewer-verdict-badge.verdict-pass" in styles
    assert ".reviewer-verdict-badge.verdict-fail" in styles
    assert ".ac-status-chip.ac-status-pass" in styles
    assert ".ac-status-chip.ac-status-fail" in styles


def test_task_detail_template_avoids_nested_backticks_in_vue_bindings():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert ':class="`' not in source


def test_sidebar_category_toggle_uses_project_scoped_accordion():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "this.cp.toggleCategory(project, cat);" in sidebar
    assert "const CATEGORY_VIEWS = ['sessions', 'tasks', 'jobs'];" in app_state
    assert "function openProjectCategory(project, view)" in app_state


def test_sidebar_task_leaf_includes_quick_actions():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")

    assert "requestTaskQuickAction(task, act, ev)" in sidebar
    assert "confirmTaskQuickAction(task, ev)" in sidebar
    assert "taskQuickConfirmMessage(task)" in sidebar
    assert "t.actions.cancel" in sidebar
    assert "t.actions.archive" in sidebar
    assert "t.actions.delete" in sidebar
    assert "requestTaskQuickAction(t, 'cancel', $event)" in sidebar
    assert "requestTaskQuickAction(t, 'archive', $event)" in sidebar
    assert "requestTaskQuickAction(t, 'delete', $event)" in sidebar
    assert "class=\"tree-inline-confirm\"" in sidebar


def test_task_section_includes_batch_quick_actions():
    task_section = Path("codepilot/web/components/TaskSection.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "selectedTaskIds" in task_section
    assert "requestBatchAction(act, ev)" in task_section
    assert "confirmBatchAction(ev)" in task_section
    assert "this.cp.taskBatchAction(ids, act);" in task_section
    assert "批量取消" in task_section
    assert "批量归档" in task_section
    assert "批量删除" in task_section
    assert "taskBatchAction," in app_state


def test_web_ui_task_cards_render_phase_progress():
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")
    task_section = Path("codepilot/web/components/TaskSection.js").read_text(encoding="utf-8")
    task_detail = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "CP.TASK_PHASE_STEPS" in utils
    assert "CP.taskPhaseProgress" in utils
    assert "下一步：Review 验收" in utils
    assert "taskPhaseProgress: CP.taskPhaseProgress" in utils
    assert "task-phase-progress" in task_section
    assert "task-phase-progress-detail" in task_detail
    assert "task-phase-progress" in project_view
    assert ".task-phase-track" in styles
    assert ".task-phase-step.state-current" in styles


def test_web_ui_bootstrap_wires_app_state_boundary_before_mount():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/boundaries/AppFeedbackBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppClarifyBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppSessionBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppSubmissionBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppStateBoundary.js\"></script>" in index_html
    assert "setup: CP.AppStateBoundary.setup," in app


def test_web_ui_daemon_banner_points_to_ui_start_command():
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")

    assert "codepilot ui start" in app
    assert "codepilot webui start" not in app


def test_app_state_rebinds_daemon_health_when_project_changes():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "watch(() => state.nav.project" in app_state
    assert "loadDaemonHealth();" in app_state
    assert "closeEventStream();" in app_state
    assert "openEventStream();" in app_state


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
    feedback_boundary = Path("codepilot/web/boundaries/AppFeedbackBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "const ACTION_KEYS = Object.freeze({" in feedback_boundary
    assert "GOAL_SUBMIT: 'goal.submit'" in feedback_boundary
    assert "COMPOSER_SUBMIT: 'composer.submit'" in feedback_boundary
    assert "SESSION_SEND: 'session.send'" in feedback_boundary
    assert "SESSION_DELETE: 'session.delete'" in feedback_boundary
    assert "SESSION_CLARIFY_REPLY: 'session.clarify.reply'" in feedback_boundary
    assert "SESSION_CLARIFY_CANCEL: 'session.clarify.cancel'" in feedback_boundary
    assert "const feedbackBoundary = CP.createAppFeedbackBoundary({ state });" in app_state
    assert "isActionPending: (actionKey) => isActionPending(actionKey)," in app_state
    assert "ACTION_KEYS," in app_state


def test_web_ui_wires_requirement_job_actions_and_filtered_events():
    feedback_boundary = Path("codepilot/web/boundaries/AppFeedbackBoundary.js").read_text(encoding="utf-8")
    submission_boundary = Path("codepilot/web/boundaries/AppSubmissionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    job_detail = Path("codepilot/web/components/JobDetail.js").read_text(encoding="utf-8")
    jobs_view = Path("codepilot/web/components/JobsView.js").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "JOB_ACTION: 'job.action'" in feedback_boundary
    assert "async function jobAction(job, action)" in submission_boundary
    assert "/api/jobs/${job.id}/${action}" in submission_boundary
    assert "jobAction," in app_state
    assert "const liveEventsForJob = (jobId) => {" in app_state
    assert "kind === 'job_log'" in app_state
    assert "scheduleRefresh();" not in app_state.split("state.liveEvents.push(event);", 2)[-1].split("const taskId", 1)[0]
    assert "canCancel()" in job_detail
    assert "@click=\"cancelJob\"" in job_detail
    assert "@click.stop=\"jobAction(j, 'retry')\"" in jobs_view
    assert "cancelling: '停止中'" in utils


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


def test_web_ui_wires_structured_clarification_fields_and_cancel_actions():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")
    fields = Path("codepilot/web/components/ClarifyFields.js").read_text(encoding="utf-8")
    goal = Path("codepilot/web/components/GoalInput.js").read_text(encoding="utf-8")
    composer = Path("codepilot/web/components/Composer.js").read_text(encoding="utf-8")
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")
    clarify_boundary = Path("codepilot/web/boundaries/AppClarifyBoundary.js").read_text(encoding="utf-8")
    session_boundary = Path("codepilot/web/boundaries/AppSessionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/components/ClarifyFields.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppClarifyBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppSessionBoundary.js\"></script>" in index_html
    assert "CP.normalizeClarifyQuestion" in utils
    assert "CP.createClarifyAnswerState" in utils
    assert "CP.exportClarifyAnswers" in utils
    assert "CP.Components.ClarifyFields" in fields
    assert "<cp-clarify-fields" in goal
    assert "<cp-clarify-fields" in composer
    assert "<cp-clarify-fields" in chat
    assert "cancelGoalClarify" in clarify_boundary
    assert "cancelComposerClarify" in clarify_boundary
    assert "cancelSessionClarify" in session_boundary
    assert "function legacyClarifyQuestionsFromMessage(message)" in clarify_boundary
    assert "legacy_q${questions.length + 1}" in clarify_boundary
    assert "const clarifyBoundary = CP.createAppClarifyBoundary({ state, pushToast });" in app_state


def test_clarify_fields_scope_radio_groups_and_single_free_text_override():
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")
    fields = Path("codepilot/web/components/ClarifyFields.js").read_text(encoding="utf-8")
    goal = Path("codepilot/web/components/GoalInput.js").read_text(encoding="utf-8")
    composer = Path("codepilot/web/components/Composer.js").read_text(encoding="utf-8")
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")

    assert "emits: ['update:answers']" in fields
    assert "groupPrefix" in fields
    assert "radioName(question)" in fields
    assert ':name="radioName(q)"' in fields
    assert "commitState(question, nextState)" in fields
    assert "this.$emit('update:answers', answers);" in fields
    assert "updateFreeText(question, value)" in fields
    assert "@update:answers=\"updateClarifyAnswers\"" in goal
    assert "@update:answers=\"updateClarifyAnswers\"" in composer
    assert "@update:answers=\"updateClarifyAnswers\"" in chat
    assert "q.type === 'single' && q.allow_free_text && text" in utils


def test_project_view_wires_batch_task_import_panel():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    batch_component = Path("codepilot/web/components/TaskBatchImport.js").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/components/TaskBatchImport.js\"></script>" in index_html
    assert "<cp-task-batch-import></cp-task-batch-import>" in project_view
    assert "CP.Components.TaskBatchImport" in batch_component
    assert "loadTaskTemplateSchema();" in batch_component
    assert "submitTaskBatch(this.validation);" in batch_component
    assert "CP.validateTaskBatchImport" in utils


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
