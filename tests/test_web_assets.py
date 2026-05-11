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


def test_task_list_uses_detail_phase_progress_style():
    task_section = Path("codepilot/web/components/TaskSection.js").read_text(encoding="utf-8")

    assert 'class="task-phase-progress task-phase-progress-detail"' in task_section
    assert "task-phase-track" not in task_section
    assert "task-phase-fill" not in task_section


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


def test_metrics_panel_renders_deepseek_balance_and_token_usage():
    metrics_panel = Path("codepilot/web/components/MetricsPanel.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "aiStatus" in app_state
    assert "loadAIStatus" in app_state
    assert "/api/ai/status" in app_state
    assert "project-status-card" in metrics_panel
    assert "project-status-grid" in metrics_panel
    assert "project-status-summary" in metrics_panel
    assert "project-usage-panel" in metrics_panel
    assert "project-usage-grid" in metrics_panel
    assert "DeepSeek" in metrics_panel
    assert "总 tokens" in metrics_panel
    assert "思考" in metrics_panel
    assert ".project-status-grid" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert ".project-usage-panel" in styles
    assert ".project-usage-grid" in styles


def test_app_state_rebinds_daemon_health_when_project_changes():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "watch(() => state.nav.project" in app_state
    assert "loadDaemonHealth();" in app_state
    assert "closeEventStream();" in app_state
    assert "openEventStream();" in app_state


def test_app_state_filters_task_state_notifications_to_current_project():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    task_state_branch = app_state.split("if (event && event.stage === 'task-state') {", 1)[1].split(
        "state.liveEvents.push(event);",
        1,
    )[0]
    assert "const extra = event.extra || {};" in task_state_branch
    assert "if (!projectMatchesCurrent(extra.project)) return;" in task_state_branch


def test_sse_remembers_last_event_id_across_project_stream_reopens():
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "CP.sseLastEventIds = CP.sseLastEventIds || {};" in utils
    assert "const streamKey = url.split('?')[0];" in utils
    assert "let lastEventId = CP.sseLastEventIds[streamKey] || '';" in utils
    assert "CP.sseLastEventIds[streamKey] = lastEventId;" in utils


def test_app_state_keeps_project_form_drafts_scoped_by_project():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "const PROJECT_DRAFTS_STORAGE_KEY = 'cp-project-drafts-v1';" in app_state
    assert "function saveProjectDraft(project" in app_state
    assert "function loadProjectDraft(project" in app_state
    assert "saveProjectDraft(prevProject);" in app_state
    assert "loadProjectDraft(partial.project);" in app_state
    for marker in (
        "goalText: state.goalText",
        "goalCategory: state.goalCategory",
        "composerMode: state.composerMode",
        "composer: cloneProjectDraftValue(state.composer)",
        "batchComposer: cloneProjectDraftValue(state.batchComposer)",
    ):
        assert marker in app_state


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
    assert "deletePending()" in chat
    assert "s.sending" not in goal
    assert "s.sending" not in composer
    assert "s.sending" not in chat


def test_chat_view_wires_streaming_session_runs_and_stop_action():
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")
    session_boundary = Path("codepilot/web/boundaries/AppSessionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "sessionRunForMessage(m)" in chat
    assert "session-process-panel" in chat
    assert "<cp-agent-log" in chat
    assert "stopSessionRun()" in chat
    assert "runtime-control-bar" in chat
    assert "s.opencodeRuntime" in chat
    assert "终止会话" in chat
    assert "Agent" in chat
    assert "agentModeOptions" in chat
    assert "agentMode" in chat
    assert "setAgentMode" in chat
    assert "CP.AGENT_MODE_OPTIONS" in Path("codepilot/web/utils.js").read_text(encoding="utf-8")
    assert "运行" not in chat
    assert "分支" not in chat
    assert "模型" not in chat
    assert "上下文" not in chat
    assert "工具权限" not in chat
    assert "run_async: true" in session_boundary
    assert "async function stopSessionRun" in session_boundary
    assert "handleSessionRunEvent(event)" in app_state
    assert "event.stage === 'session-run'" in app_state
    assert "sessionRuns: {}" in app_state
    assert "opencodeRuntime" in app_state
    assert ".session-process-panel" in styles
    assert ".chat-input-bar.is-streaming" in styles
    assert ".runtime-control-bar" in styles
    assert ".embedded-messages" in styles
    assert "overflow-y: auto" in styles


def test_project_view_uses_unified_workbench_layout():
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "project-workbench-grid" in project_view
    assert "project-session-list" in project_view
    assert "cp.selectEmbeddedSession" in project_view
    assert "project-service-panel" in project_view
    assert "project-context-rail" in project_view
    assert "project-metrics-panel" in project_view
    assert "会话就是和 OpenCode 的交互" in project_view
    assert "intake-segmented" not in project_view
    assert "s.composerMode === 'batch'" not in project_view
    assert "cp-composer" not in project_view
    assert "cp-task-batch-import" not in project_view
    assert ".project-workbench-grid" in styles
    assert ".project-service-panel" in styles
    assert ".opencode-session-card" in styles
    assert "@media (max-width: 1280px)" in styles
    assert "grid-template-columns: 220px minmax(420px, 1fr);" in styles
    assert "grid-column: 1 / -1;" in styles
    assert ".project-metrics-panel" in styles
    assert "grid-column: span 2;" in styles
    assert "max-height: 280px;\n    overflow-y: auto;" in styles


def test_project_workbench_embeds_streaming_session_chat_instead_of_goal_form():
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")
    session_boundary = Path("codepilot/web/boundaries/AppSessionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "cp-goal-input" not in project_view
    assert "<cp-session-chat-panel" in project_view
    assert "project-session-workbench" in project_view
    assert "intake-segmented" not in project_view
    assert "CP.Components.SessionChatPanel" in chat
    assert "async function sendEmbeddedChat" in session_boundary
    assert "ensureProjectSessionForSend" in session_boundary
    assert "`/api/sessions/${sessionId}/messages`" in session_boundary
    assert "run_async: true" in session_boundary
    assert "activeProjectSessionId" in app_state
    assert "selectEmbeddedSession" in app_state
    assert "openSessionPage" in app_state


def test_sidebar_removes_session_category_with_advanced_page_escape():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")

    assert "会话" not in sidebar
    assert "selectEmbeddedSession" not in sidebar
    assert "projectSessions(" not in sidebar
    assert "sessionCount(" not in sidebar
    assert "newSession(p.name)" not in sidebar
    assert "cp.openSessionPage" in project_view
    assert "会话详情" in project_view
    assert "打开高级页" not in project_view


def test_web_ui_views_share_console_toolbar_contract():
    tasks_view = Path("codepilot/web/components/TasksView.js").read_text(encoding="utf-8")
    sessions_view = Path("codepilot/web/components/SessionsView.js").read_text(encoding="utf-8")
    jobs_view = Path("codepilot/web/components/JobsView.js").read_text(encoding="utf-8")
    task_detail = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    job_detail = Path("codepilot/web/components/JobDetail.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    for source in (tasks_view, sessions_view, jobs_view, task_detail, job_detail):
        assert "view-toolbar" in source
    assert ".view-toolbar" in styles
    assert ".ops-panel" in styles
    assert ".detail-grid" in styles


def test_sessions_view_wires_history_search():
    sessions = Path("codepilot/web/components/SessionsView.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "searchQuery" in sessions
    assert "runSearch()" in sessions
    assert "/api/sessions?${qs.toString()}" in sessions
    assert "搜索会话标题、消息正文或任务编号" in sessions
    assert "session-snippet" in sessions
    assert ".session-search-input" in styles
    assert ".session-snippet" in styles


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
    assert "<cp-clarify-fields" not in chat
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
    assert "@update:answers=\"updateClarifyAnswers\"" not in chat
    assert "q.type === 'single' && q.allow_free_text && text" in utils


def test_batch_task_import_component_remains_registered_but_not_on_opencode_workspace():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    batch_component = Path("codepilot/web/components/TaskBatchImport.js").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/components/TaskBatchImport.js\"></script>" in index_html
    assert "<cp-task-batch-import></cp-task-batch-import>" not in project_view
    assert "CP.Components.TaskBatchImport" in batch_component
    assert "loadTaskTemplateSchema();" in batch_component
    assert "submitTaskBatch(this.validation);" in batch_component
    assert "CP.validateTaskBatchImport" in utils


def test_batch_task_import_panel_shows_full_template_structure():
    batch_component = Path("codepilot/web/components/TaskBatchImport.js").read_text(encoding="utf-8")

    assert "templateHeadings()" in batch_component
    assert "schema.template_markdown" in batch_component
    assert "完整模板结构" in batch_component
    assert "v-for=\"label in templateHeadings\"" in batch_component


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


def test_web_ui_professional_console_style_contract():
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "professional-shell" in app
    for token in (
        "--surface:",
        "--surface-raised:",
        "--sidebar:",
        "--shadow-md:",
        "--focus-soft:",
    ):
        assert token in styles

    for selector in (
        ".professional-shell",
        ".main-header::after",
        ".content-pane::before",
        ".card-head::before",
        ".task-item::before",
        ".service-row::before",
        ".metric::before",
        ".big-empty svg",
        "[data-theme=\"dark\"] .professional-shell",
    ):
        assert selector in styles

    assert ".shell { grid-template-columns: minmax(260px, 304px) minmax(0, 1fr);" in styles
    assert ".view { width: min(1440px, 100%);" in styles
    assert "@media (max-width: 720px)" in styles
    assert ".main-header { min-height: 60px;" in styles
