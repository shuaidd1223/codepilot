/* App-level boundary orchestrator extracted from app.js.
 * Wires navigation, shared state, loaders, SSE refresh, and sub-boundaries into one stable cp API. */
/* global Vue, CP */

window.CP = window.CP || {};

CP.AppStateBoundary = CP.AppStateBoundary || (() => {
  const { reactive, computed, onMounted, onUnmounted, nextTick, provide, watch } = Vue;

  function setup() {
    const state = reactive({
      projects: [], tasks: [], jobs: [], events: [], sessions: [],
      tasksByProject: {}, jobsByProject: {},
      taskBoard: { project: '', columns: [], counts: {}, total: 0, sort: { column_order: [] } },
      taskBoardByProject: {},

      nav: { project: null, view: 'overview', id: null },
      expanded: {},

      taskDetail: null,
      taskDetailLoading: false,
      taskDetailError: '',
      sessionDetail: null,
      sessionMessages: [],
      sessionMessagesLoading: false,
      sessionRuns: {},
      activeProjectSessionId: null,

      taskLog: { taskId: null, text: '', nextOffset: 0, size: 0, done: true, loading: false },
      taskRunStatus: null,

      autoRefresh: true, timer: null,
      loading: false, sending: false, newSessionLoading: false,
      actionPending: {},
      workflowPending: '',
      projectSubmitting: false, deletingProject: '',
      servicePending: '',
      pendingTasks: {},
      dark: false,
      toasts: [], toastSeq: 0,
      confirmDialog: {
        open: false,
        title: '',
        message: '',
        confirmText: '确认',
        cancelText: '取消',
        tone: 'danger',
      },

      liveEvents: [],

      daemonHealth: { alive: true, running: false, pid: 0, reason: '', stale_seconds: 0 },
      aiStatus: { ok: true, providers: {}, balances: {}, usage: {} },

      projectForm: { open: false, path: '', name: '', noConfig: false },
      opencodeRuntime: {
        agentMode: 'codepilot',
        permissionMode: 'ask',
      },
      projectDrafts: {},
      chatText: '',
    });

    const feedbackBoundary = CP.createAppFeedbackBoundary({ state });
    const ACTION_KEYS = feedbackBoundary.ACTION_KEYS;
    const pushToast = feedbackBoundary.pushToast;
    const dismissToast = feedbackBoundary.dismissToast;
    const confirmDialog = feedbackBoundary.confirmDialog;
    const resolveConfirm = feedbackBoundary.resolveConfirm;
    const registerChatScroll = feedbackBoundary.registerChatScroll;
    const getChatScrollEl = feedbackBoundary.getChatScrollEl;
    const isActionPending = feedbackBoundary.isActionPending;
    const isRefreshBlocked = feedbackBoundary.isRefreshBlocked;
    const runScopedAction = feedbackBoundary.runScopedAction;

    const currentProject = computed(() =>
      state.projects.find((project) => project.name === state.nav.project) || null
    );

    const metrics = computed(() => {
      const stats = (currentProject.value && currentProject.value.stats) || {};
      return [
        { label: '进行中', value: stats.in_progress || 0, tone: 'info' },
        { label: '待办', value: stats.backlog || 0, tone: '' },
        { label: '失败 / 取消', value: (stats.failed || 0) + (stats.cancelled || 0), tone: 'danger' },
        { label: '已完成', value: stats.done || 0, tone: 'success' },
        { label: '总任务', value: stats.total || 0, tone: '' },
      ];
    });

    const projectTasks = computed(() => state.tasks);
    const projectSessions = computed(() =>
      state.sessions.filter((session) => !state.nav.project || session.project === state.nav.project)
    );
    const projectJobs = computed(() => state.jobs);

    const currentJob = computed(() => {
      if (state.nav.view !== 'job' || !state.nav.id) return null;
      return state.jobs.find((job) => job.id === state.nav.id) || null;
    });

    const NAV_STORAGE_KEY = 'cp-nav-v1';
    const EXPAND_STORAGE_KEY = 'cp-expand-v1';
    const PROJECT_DRAFTS_STORAGE_KEY = 'cp-project-drafts-v1';
    const CATEGORY_VIEWS = ['sessions', 'tasks', 'jobs'];
    const FALLBACK_REFRESH_MS = 30000;
    const REFRESH_DEBOUNCE_MS = 250;
    const LIVE_EVENTS_MAX = 10000;

    let dashboardReqSeq = 0;
    let navSyncing = false;
    let refreshTimer = 0;
    let sseHandle = null;
    let mounted = false;
    let taskDetailBoundary = null;
    let sessionBoundary = null;
    let submissionBoundary = null;

    const categoryKey = (project, view) => CP.StateBoundary.categoryKey(project, view);

    function navToHash(nav) {
      return CP.StateBoundary.navToHash(nav);
    }

    function navFromHash(hash) {
      return CP.StateBoundary.navFromHash(hash);
    }

    function persistNav() {
      try { localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify(state.nav)); } catch (_e) { /* ignore */ }
      const target = navToHash(state.nav);
      if (target && target !== location.hash) {
        navSyncing = true;
        try { history.replaceState(null, '', target); } finally { navSyncing = false; }
      }
    }

    function readStoredNav() {
      const fromHash = navFromHash(location.hash);
      if (fromHash) return fromHash;
      try {
        const raw = localStorage.getItem(NAV_STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && parsed.project) return parsed;
      } catch (_e) { /* ignore */ }
      return null;
    }

    function cloneProjectDraftValue(value) {
      if (value == null) return value;
      try {
        return JSON.parse(JSON.stringify(value));
      } catch (_e) {
        return value;
      }
    }

    function defaultProjectDraft() {
      return {
        activeProjectSessionId: null,
        opencodeRuntime: {
          agentMode: 'codepilot',
          permissionMode: 'ask',
        },
        chatText: '',
      };
    }

    function normalizeProjectDraft(draft) {
      const fallback = defaultProjectDraft();
      const source = (draft && typeof draft === 'object') ? draft : {};
      const opencodeRuntime = (source.opencodeRuntime && typeof source.opencodeRuntime === 'object') ? source.opencodeRuntime : {};
      const activeProjectSessionId = Number(source.activeProjectSessionId);
      return {
        activeProjectSessionId: Number.isFinite(activeProjectSessionId) && activeProjectSessionId > 0
          ? activeProjectSessionId
          : fallback.activeProjectSessionId,
        opencodeRuntime: {
          agentMode: (() => {
            const raw = typeof opencodeRuntime.agentMode === 'string' ? opencodeRuntime.agentMode : '';
            const allowed = (window.CP && CP.AGENT_MODE_KEYS) || [];
            return allowed.includes(raw) ? raw : fallback.opencodeRuntime.agentMode;
          })(),
          permissionMode: (() => {
            const raw = typeof opencodeRuntime.permissionMode === 'string' ? opencodeRuntime.permissionMode : '';
            const allowed = (window.CP && CP.PERMISSION_MODE_KEYS) || [];
            return allowed.includes(raw) ? raw : fallback.opencodeRuntime.permissionMode;
          })(),
        },
        chatText: typeof source.chatText === 'string' ? source.chatText : fallback.chatText,
      };
    }

    function currentProjectDraftSnapshot() {
      return {
        activeProjectSessionId: state.activeProjectSessionId,
        opencodeRuntime: cloneProjectDraftValue(state.opencodeRuntime),
        chatText: state.chatText,
      };
    }

    function persistProjectDrafts() {
      try { localStorage.setItem(PROJECT_DRAFTS_STORAGE_KEY, JSON.stringify(state.projectDrafts)); } catch (_e) { /* ignore */ }
    }

    function readStoredProjectDrafts() {
      try {
        const raw = localStorage.getItem(PROJECT_DRAFTS_STORAGE_KEY);
        if (!raw) return;
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          Object.assign(state.projectDrafts, parsed);
        }
      } catch (_e) { /* ignore */ }
    }

    function saveProjectDraft(project = state.nav.project) {
      if (!project) return;
      state.projectDrafts[project] = currentProjectDraftSnapshot();
      persistProjectDrafts();
    }

    function loadProjectDraft(project = state.nav.project) {
      const draft = normalizeProjectDraft(project ? state.projectDrafts[project] : null);
      state.activeProjectSessionId = draft.activeProjectSessionId;
      state.opencodeRuntime = draft.opencodeRuntime;
      state.chatText = draft.chatText;
    }

    function deleteProjectDraft(project) {
      if (!project || !Object.prototype.hasOwnProperty.call(state.projectDrafts, project)) return;
      delete state.projectDrafts[project];
      persistProjectDrafts();
    }

    let projectDraftSaveQueued = false;
    function queueProjectDraftSave() {
      if (!state.nav.project || projectDraftSaveQueued) return;
      projectDraftSaveQueued = true;
      requestAnimationFrame(() => {
        projectDraftSaveQueued = false;
        saveProjectDraft(state.nav.project);
      });
    }

    function openProjectCategory(project, view) {
      CP.StateBoundary.openProjectCategory(state, project, view, CATEGORY_VIEWS);
    }

    function normalizeProjectCategoryExpanded(project) {
      CP.StateBoundary.normalizeProjectCategoryExpanded(state, project, CATEGORY_VIEWS);
    }

    function viewToCategory(view) {
      return CP.StateBoundary.viewToCategory(view, CATEGORY_VIEWS);
    }

    function applyRestoredNav(restored) {
      if (!restored || !restored.project) return;
      setNav({
        project: restored.project,
        view: restored.view || 'overview',
        id: restored.id ?? null,
      });
      state.expanded[restored.project] = true;
      const restoredCategory = viewToCategory(restored.view);
      if (restoredCategory) openProjectCategory(restored.project, restoredCategory);
      if (state.nav.view === 'task' && state.nav.id) {
        loadTaskDetail();
        loadTaskLog(state.nav.id, { reset: true });
      } else if (state.nav.view === 'session' && state.nav.id) {
        loadSessionChat();
      }
    }

    function setNav(partial) {
      const prevProject = state.nav.project;
      if (partial.project !== undefined && partial.project !== prevProject) {
        saveProjectDraft(prevProject);
      }
      Object.assign(state.nav, partial);
      if (partial.project !== undefined && partial.project !== prevProject) {
        CP.StateBoundary.pivotProjectAliases(state, partial.project);
        loadProjectDraft(partial.project);
      }
      persistNav();
    }

    function toggleExpanded(key) {
      state.expanded[key] = !state.expanded[key];
    }

    function isExpanded(key, defaultOpen = false) {
      return state.expanded[key] === undefined ? defaultOpen : state.expanded[key];
    }

    function selectProject(name) {
      state.expanded[name] = true;
      setNav({ project: name, view: 'overview', id: null });
    }

    function toggleProject(name) {
      state.expanded[name] = !state.expanded[name];
    }

    function toggleCategory(project, view) {
      state.expanded[project] = true;
      const key = categoryKey(project, view);
      const willOpen = !isExpanded(key, false);
      if (willOpen) openProjectCategory(project, view);
      else state.expanded[key] = false;
    }

    function selectCategory(project, view) {
      state.expanded[project] = true;
      openProjectCategory(project, view);
      setNav({ project, view, id: null });
    }

    function selectSession(project, id) {
      return selectEmbeddedSession(project, id);
    }

    function selectEmbeddedSession(project, id) {
      return ensureSessionBoundary().selectEmbeddedSession(project, id);
    }

    function openSessionPage(project, id) {
      return ensureSessionBoundary().openSessionPage(project, id);
    }

    function selectSessionPage(project, id) {
      state.expanded[project] = true;
      openProjectCategory(project, 'sessions');
      setNav({ project, view: 'session', id });
      loadSessionChat();
    }

    function selectTask(project, id) {
      state.expanded[project] = true;
      openProjectCategory(project, 'tasks');
      setNav({ project, view: 'task', id });
      loadTaskDetail();
      loadTaskLog(id, { reset: true });
    }

    function selectJob(project, id) {
      state.expanded[project] = true;
      openProjectCategory(project, 'jobs');
      setNav({ project, view: 'job', id });
    }

    function toggleAuto() {
      state.autoRefresh = !state.autoRefresh;
      schedule();
    }

    function toggleDark() {
      state.dark = !state.dark;
      document.documentElement.dataset.theme = state.dark ? 'dark' : 'light';
      try { localStorage.setItem('cp-dark', state.dark ? '1' : '0'); } catch (_e) { /* ignore */ }
    }

    async function loadDashboard() {
      if (isRefreshBlocked()) return;
      const reqId = ++dashboardReqSeq;
      state.loading = true;
      try {
        const data = await CP.api.get('/api/projects');
        if (reqId !== dashboardReqSeq) return;
        state.projects = data.projects || [];
        state.events = data.events || [];
        state.tasksByProject = data.tasks_by_project || {};
        state.taskBoardByProject = data.task_board_by_project || {};
        state.jobsByProject = data.jobs_by_project || {};
        const active = state.nav.project || data.selected_project || null;
        CP.StateBoundary.pivotProjectAliases(state, active);
        if (!state.nav.project && data.selected_project) {
          setNav({ project: data.selected_project, view: state.nav.view || 'overview', id: state.nav.id ?? null });
          state.expanded[data.selected_project] = true;
        }
        await loadSessions();
        if (reqId !== dashboardReqSeq) return;
        if (state.nav.view === 'task' && state.nav.id) {
          if (!state.tasks.find((task) => task.id === state.nav.id)) {
            setNav({ view: 'overview', id: null });
          } else {
            await loadTaskDetail({ silent: true });
          }
        }
        loadAIStatus();
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        if (reqId === dashboardReqSeq) {
          state.loading = false;
        }
      }
    }

    function ensureTaskDetailBoundary() {
      if (taskDetailBoundary) return taskDetailBoundary;
      taskDetailBoundary = CP.createTaskDetailBoundary({
        state,
        pushToast,
        loadDashboard,
      });
      return taskDetailBoundary;
    }

    async function loadTaskDetail(options = {}) {
      return ensureTaskDetailBoundary().loadTaskDetail(options);
    }

    function handleTaskLogStreamEvent(event) {
      return ensureTaskDetailBoundary().handleTaskLogStreamEvent(event);
    }

    function handleTaskRunStatusEvent(event) {
      return ensureTaskDetailBoundary().handleTaskRunStatusEvent(event);
    }

    async function loadTaskLog(taskId, options = {}) {
      return ensureTaskDetailBoundary().loadTaskLog(taskId, options);
    }

    async function loadDaemonHealth() {
      try {
        const qs = state.nav.project ? `?project=${encodeURIComponent(state.nav.project)}` : '';
        const data = await CP.api.get(`/api/daemon/health${qs}`);
        state.daemonHealth = data || state.daemonHealth;
      } catch (_e) { /* silent */ }
    }

    async function loadAIStatus(options = {}) {
      try {
        const refresh = options.refresh ? '1' : '0';
        const qs = state.nav.project
          ? `?project=${encodeURIComponent(state.nav.project)}&refresh=${refresh}`
          : `?refresh=${refresh}`;
        const data = await CP.api.get(`/api/ai/status${qs}`);
        state.aiStatus = data || state.aiStatus;
      } catch (_e) { /* silent */ }
    }

    async function loadSessions() {
      try {
        const data = await CP.api.get('/api/sessions');
        state.sessions = data.sessions || [];
        if (state.nav.project && state.nav.view === 'overview') {
          ensureProjectSessionSelected(state.nav.project, { load: true });
        }
      } catch (_e) { /* silent */ }
    }

    function ensureSessionBoundary() {
      if (sessionBoundary) return sessionBoundary;
      sessionBoundary = CP.createAppSessionBoundary({
        state,
        nextTick,
        pushToast,
        runScopedAction,
        ACTION_KEYS,
        getChatScrollEl,
        loadSessions,
        confirmDialog,
        setNav,
        openProjectCategory,
      });
      return sessionBoundary;
    }

    async function loadSessionChat(sessionId = null) {
      return ensureSessionBoundary().loadSessionChat(sessionId);
    }

    function ensureProjectSessionSelected(project = state.nav.project, options = {}) {
      return ensureSessionBoundary().ensureProjectSessionSelected(project, options);
    }

    async function newSession(projectOrOptions = null, options = {}) {
      return ensureSessionBoundary().newSession(projectOrOptions, options);
    }

    async function sendChat() {
      return ensureSessionBoundary().sendChat();
    }

    async function sendEmbeddedChat() {
      return ensureSessionBoundary().sendEmbeddedChat();
    }

    async function stopSessionRun(messageId = null) {
      return ensureSessionBoundary().stopSessionRun(messageId);
    }

    async function deleteSession() {
      return ensureSessionBoundary().deleteSession();
    }

    async function taskAction(taskId, action) {
      return ensureTaskDetailBoundary().taskAction(taskId, action);
    }

    async function taskBatchAction(taskIds, action) {
      return ensureTaskDetailBoundary().taskBatchAction(taskIds, action);
    }

    function ensureSubmissionBoundary() {
      if (submissionBoundary) return submissionBoundary;
      submissionBoundary = CP.createAppSubmissionBoundary({
        state,
        pushToast,
        runScopedAction,
        ACTION_KEYS,
        loadDashboard,
        selectProject,
        setNav,
        confirmDialog,
        deleteProjectDraft,
      });
      return submissionBoundary;
    }

    function toggleProjectForm(open = null) {
      return ensureSubmissionBoundary().toggleProjectForm(open);
    }

    async function submitProject() {
      return ensureSubmissionBoundary().submitProject();
    }

    async function deleteProject(name) {
      return ensureSubmissionBoundary().deleteProject(name);
    }

    async function renameProject(name, newName) {
      return ensureSubmissionBoundary().renameProject(name, newName);
    }

    async function projectService(service, action) {
      return ensureSubmissionBoundary().projectService(service, action);
    }

    async function runInspectWorkflow(project = state.nav.project) {
      if (!project || state.workflowPending) return;
      state.workflowPending = 'inspect';
      try {
        const data = await CP.api.post(`/api/projects/${encodeURIComponent(project)}/inspect/runs`, {});
        pushToast('巡检工作流已生成', 'success');
        await loadDashboard();
        return data;
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.workflowPending = '';
      }
    }

    async function workflowAction(actionId, project = state.nav.project) {
      if (!project || !actionId || state.workflowPending) return;
      state.workflowPending = actionId;
      try {
        const data = await CP.api.post('/api/workflow/actions', { project, action_id: actionId });
        pushToast('工作流动作已执行', 'success');
        await loadDashboard();
        return data;
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.workflowPending = '';
      }
    }

    async function workflowAutoAction(project = state.nav.project) {
      if (!project || state.workflowPending) return;
      state.workflowPending = 'workflow-auto';
      try {
        const data = await CP.api.post('/api/workflow/actions', { project, auto: true });
        const actionId = data && data.action && data.action.id;
        pushToast(actionId ? `已自动推进: ${actionId}` : '暂无可自动推进动作', actionId ? 'success' : 'info');
        await loadDashboard();
        return data;
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.workflowPending = '';
      }
    }

    async function jobAction(job, action) {
      return ensureSubmissionBoundary().jobAction(job, action);
    }

    function projectMatchesCurrent(project) {
      return !project || !state.nav.project || project === state.nav.project;
    }

    function mergeJobPayload(payload) {
      if (!payload || payload.id == null) return;
      const id = Number(payload.id);
      const idx = state.jobs.findIndex((job) => Number(job.id) === id);
      if (idx >= 0) state.jobs[idx] = { ...state.jobs[idx], ...payload };
      const bucket = state.jobsByProject[state.nav.project] || [];
      const bucketIdx = bucket.findIndex((job) => Number(job.id) === id);
      if (bucketIdx >= 0) bucket[bucketIdx] = { ...bucket[bucketIdx], ...payload };
    }

    function appendJobLogPayload(payload) {
      if (!payload || payload.id == null || !payload.line) return;
      const id = Number(payload.id);
      const applyLine = (job) => {
        const log = Array.isArray(job.log) ? job.log.slice() : [];
        if (log[log.length - 1] !== payload.line) log.push(payload.line);
        return {
          ...job,
          log: log.slice(-50),
          updated_at: payload.updated_at || job.updated_at,
        };
      };
      const idx = state.jobs.findIndex((job) => Number(job.id) === id);
      if (idx >= 0) state.jobs[idx] = applyLine(state.jobs[idx]);
      const bucket = state.jobsByProject[state.nav.project] || [];
      const bucketIdx = bucket.findIndex((job) => Number(job.id) === id);
      if (bucketIdx >= 0) bucket[bucketIdx] = applyLine(bucket[bucketIdx]);
    }

    function handleUiStateEvent(event) {
      const extra = (event && event.extra) || {};
      const kind = String(extra.kind || '');
      const project = String(extra.project || '');
      const payload = extra.payload || {};
      if (!projectMatchesCurrent(project)) return true;
      if (kind === 'job_log') {
        appendJobLogPayload(payload);
        return true;
      }
      if (kind === 'job') {
        mergeJobPayload(payload);
        scheduleRefresh();
        return true;
      }
      if (kind === 'event') {
        state.events = [payload, ...(state.events || [])].filter(Boolean).slice(0, 12);
        return true;
      }
      return false;
    }

    function sessionRunEventLine(event, extra) {
      const ts = (event && event.timestamp || '').slice(11, 19) || '--:--:--';
      const type = String((event && event.type) || 'summary');
      const msg = String((event && event.message) || '');
      const delta = String((extra && extra.content_delta) || '');
      const error = String((extra && extra.error) || '');
      const tools = Array.isArray(extra && extra.tool_calls) ? extra.tool_calls : [];
      const latestTool = tools.length ? ` tools=${tools.map((tool) => tool.name || tool.tool || '-').join(',')}` : '';
      const body = type === 'error' ? (error || delta || msg) : (delta || msg || error);
      return `[${ts}] ${type}${latestTool}${body ? ` ${body}` : ''}`;
    }

    function handleSessionRunEvent(event) {
      const extra = (event && event.extra) || {};
      if (!projectMatchesCurrent(String(extra.project || ''))) return true;
      const sessionId = Number(extra.session_id || 0);
      const messageId = Number(extra.assistant_message_id || 0);
      if (!sessionId || !messageId) return true;
      const key = String(messageId);
      const previous = state.sessionRuns[key] || {};
      const events = Array.isArray(previous.events) ? previous.events.slice() : [];
      events.push({
        id: event && event.id,
        timestamp: event && event.timestamp,
        type: event && event.type,
        message: event && event.message,
        extra,
      });
      const status = String(extra.status || previous.status || 'running');
      const nextRun = {
        ...previous,
        session_id: sessionId,
        assistant_message_id: messageId,
        status,
        content_snapshot: String(extra.content_snapshot ?? previous.content_snapshot ?? ''),
        tool_calls: Array.isArray(extra.tool_calls) ? extra.tool_calls : (previous.tool_calls || []),
        opencode_session_id: String(extra.opencode_session_id || previous.opencode_session_id || ''),
        error: String(extra.error || previous.error || ''),
        events: events.slice(-200),
        raw_log: [...(Array.isArray(previous.raw_log) ? previous.raw_log : []), sessionRunEventLine(event, extra)].slice(-300),
      };
      state.sessionRuns[key] = nextRun;
      const isVisibleSession = (
        (state.nav.view === 'session' && Number(state.nav.id) === sessionId)
        || (state.nav.view === 'overview' && Number(state.activeProjectSessionId || 0) === sessionId)
      );
      if (isVisibleSession) {
        const idx = state.sessionMessages.findIndex((msg) => Number(msg.id) === messageId);
        if (idx >= 0) {
          const current = state.sessionMessages[idx] || {};
          const content = nextRun.content_snapshot || (status === 'error' ? nextRun.error : '') || current.content || '';
          const finalIntent = status === 'done'
            ? 'opencode'
            : (status === 'error' ? 'error' : (status === 'cancelled' ? 'cancelled' : 'streaming'));
          state.sessionMessages[idx] = {
            ...current,
            content,
            intent: finalIntent,
            metadata: {
              ...(current.metadata || {}),
              status,
              tool_calls: nextRun.tool_calls,
              opencode_session_id: nextRun.opencode_session_id,
              error: nextRun.error,
            },
          };
        } else {
          loadSessionChat(sessionId);
        }
      }
      if (['done', 'error', 'cancelled'].includes(status)) {
        loadSessions();
      }
      return true;
    }

    function runRefresh() {
      if (isRefreshBlocked()) return;
      loadDashboard();
      if (state.nav.view === 'session' && state.nav.id) loadSessionChat();
      else if (state.nav.view === 'overview' && state.activeProjectSessionId) loadSessionChat(state.activeProjectSessionId);
    }

    function scheduleRefresh({ immediate = false } = {}) {
      if (immediate) {
        if (refreshTimer) {
          clearTimeout(refreshTimer);
          refreshTimer = 0;
        }
        runRefresh();
        return;
      }
      if (refreshTimer) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = 0;
        runRefresh();
      }, REFRESH_DEBOUNCE_MS);
    }

    function schedule() {
      clearInterval(state.timer);
      if (!state.autoRefresh) return;
      state.timer = setInterval(() => scheduleRefresh(), FALLBACK_REFRESH_MS);
    }

    function openEventStream() {
      if (sseHandle) return;
      const qs = state.nav.project ? `?project=${encodeURIComponent(state.nav.project)}` : '';
      sseHandle = CP.sse.open(`/api/events/stream${qs}`, (event) => {
        if (handleTaskLogStreamEvent(event)) return;
        if (handleTaskRunStatusEvent(event)) return;
        if (event && event.stage === 'daemon-health' && event.extra) {
          state.daemonHealth = event.extra;
          return;
        }
        if (event && event.stage === 'session-run' && handleSessionRunEvent(event)) {
          return;
        }
        if (event && (event.stage === 'ui-state' || event.stage === 'job-log') && handleUiStateEvent(event)) {
          state.liveEvents.push(event);
          if (state.liveEvents.length > LIVE_EVENTS_MAX) {
            state.liveEvents.splice(0, state.liveEvents.length - LIVE_EVENTS_MAX);
          }
          return;
        }
        if (event && event.stage === 'task-state') {
          const extra = event.extra || {};
          if (!projectMatchesCurrent(extra.project)) return;
          const changes = Array.isArray(extra.changes) ? extra.changes : [];
          scheduleRefresh({ immediate: true });
          const taskId = state.nav.view === 'task' ? state.nav.id : null;
          if (taskId) {
            const ids = Array.isArray(extra.changed_task_ids) ? extra.changed_task_ids : [];
            if (!ids.length || ids.some((id) => Number(id) === Number(taskId))) {
              loadTaskDetail({ silent: true });
              loadTaskLog(taskId);
            }
          }
          changes.forEach((change) => {
            const task = change && change.task ? change.task : {};
            const status = String(task.status || '');
            const title = String(task.title || '').trim();
            if (status === 'done') pushToast(`任务已完成：${title || ('#' + task.id)}`, 'success');
            else if (status === 'failed') pushToast(`任务失败：${title || ('#' + task.id)}`, 'error');
            else if (status === 'cancelled') pushToast(`任务已取消：${title || ('#' + task.id)}`, 'warning');
            else if (status === 'in_progress') pushToast(`任务开始执行：${title || ('#' + task.id)}`, 'info');
          });
          return;
        }
        state.liveEvents.push(event);
        if (state.liveEvents.length > LIVE_EVENTS_MAX) {
          state.liveEvents.splice(0, state.liveEvents.length - LIVE_EVENTS_MAX);
        }
        const taskId = state.nav.view === 'task' ? state.nav.id : null;
        if (taskId && (event.task_id === taskId || event.task_id == null)) {
          loadTaskLog(taskId);
        }
      });
    }

    function closeEventStream() {
      if (sseHandle) {
        sseHandle.close();
        sseHandle = null;
      }
    }

    let _projectSwitchSeq = 0;
    watch(() => state.nav.project, async (nextProject, prevProject) => {
      if (nextProject === prevProject) return;
      const seq = ++_projectSwitchSeq;
      loadDaemonHealth();
      loadAIStatus({ refresh: true });
      if (!mounted) return;
      closeEventStream();
      openEventStream();
      // 切换项目：清除旧会话数据，选中新项目的最新会话
      state.activeProjectSessionId = null;
      state.sessionDetail = null;
      state.sessionMessages = [];
      scheduleRefresh({ immediate: true });
      await nextTick();
      // 防止快速切换导致旧请求覆盖新数据
      if (seq !== _projectSwitchSeq) return;
      if (state.nav.view === 'overview') {
        await ensureProjectSessionSelected(nextProject, { load: true });
      }
    });

    function installKeyboardShortcuts() {
      document.addEventListener('keydown', (event) => {
        const tag = (event.target && event.target.tagName) || '';
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || (event.target && event.target.isContentEditable)) return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === '?') {
          pushToast('快捷键: d=切换深色 · r=手动刷新 · n=新建会话 · /=聚焦输入', 'info');
          event.preventDefault();
          return;
        }
        if (event.key === 'd') {
          toggleDark();
          event.preventDefault();
          return;
        }
        if (event.key === 'r') {
          loadDashboard();
          event.preventDefault();
          return;
        }
        if (event.key === 'n') {
          newSession();
          event.preventDefault();
          return;
        }
        if (event.key === '/') {
          const input = document.querySelector('.chat-textarea');
          if (input) {
            input.focus();
            event.preventDefault();
          }
        }
      });
    }

    function onHashChange() {
      if (navSyncing) return;
      const parsed = navFromHash(location.hash);
      if (!parsed) return;
      if (
        parsed.project === state.nav.project
        && parsed.view === state.nav.view
        && parsed.id === state.nav.id
      ) return;
      applyRestoredNav(parsed);
    }

    onMounted(() => {
      mounted = true;
      try { state.dark = localStorage.getItem('cp-dark') === '1'; } catch (_e) { /* ignore */ }
      document.documentElement.dataset.theme = state.dark ? 'dark' : 'light';

      try {
        const raw = localStorage.getItem(EXPAND_STORAGE_KEY);
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === 'object') {
            Object.assign(state.expanded, parsed);
          }
        }
      } catch (_e) { /* ignore */ }

      readStoredProjectDrafts();

      let expandSaveQueued = false;
      watch(() => state.expanded, () => {
        if (expandSaveQueued) return;
        expandSaveQueued = true;
        requestAnimationFrame(() => {
          expandSaveQueued = false;
          try {
            localStorage.setItem(EXPAND_STORAGE_KEY, JSON.stringify(state.expanded));
          } catch (_e) { /* ignore */ }
        });
      }, { deep: true });

      const restored = readStoredNav();
      if (restored && restored.project) {
        setNav({
          project: restored.project,
          view: restored.view || 'overview',
          id: restored.id ?? null,
        });
        state.expanded[restored.project] = true;
        const restoredCategory = viewToCategory(state.nav.view);
        if (restoredCategory) openProjectCategory(restored.project, restoredCategory);
      }

      watch(() => [
        state.activeProjectSessionId,
        state.opencodeRuntime,
        state.chatText,
      ], queueProjectDraftSave, { deep: true });

      loadDashboard().then(() => {
        for (const project of state.projects || []) {
          normalizeProjectCategoryExpanded(project && project.name);
        }
        if (state.nav.view === 'task' && state.nav.id) {
          loadTaskDetail();
          loadTaskLog(state.nav.id, { reset: true });
        } else if (state.nav.view === 'session' && state.nav.id) {
          loadSessionChat();
        }
      });

      loadDaemonHealth();
      loadAIStatus({ refresh: true });
      schedule();
      openEventStream();
      installKeyboardShortcuts();
      window.addEventListener('hashchange', onHashChange);
    });

    onUnmounted(() => {
      mounted = false;
      saveProjectDraft(state.nav.project);
      clearInterval(state.timer);
      closeEventStream();
      window.removeEventListener('hashchange', onHashChange);
    });

    const liveEventsForJob = (jobId) => {
      const id = Number(jobId);
      const job = state.jobs.find((item) => Number(item.id) === id) || {};
      const taskIds = new Set((Array.isArray(job.task_ids) ? job.task_ids : []).map((value) => Number(value)));
      return state.liveEvents.filter((event) => {
        if (!event) return false;
        const extra = event.extra || {};
        const kind = String(extra.kind || '');
        const payload = extra.payload || {};
        if ((event.stage === 'job-log' || event.stage === 'ui-state') && (kind === 'job_log' || kind === 'job')) {
          return Number(payload.id) === id;
        }
        if (event.task_id != null) return taskIds.has(Number(event.task_id));
        return false;
      });
    };

    const liveEventsForTask = (taskId) => {
      if (!taskId) return [];
      return state.liveEvents.filter((event) => event && (event.task_id === taskId || event.task_id == null));
    };

    const sessionRunForMessage = (message) => {
      const id = message && message.id;
      if (!id) return null;
      return state.sessionRuns[String(id)] || null;
    };

    const activeSessionRun = computed(() => {
      const runs = Object.values(state.sessionRuns || {});
      const sessionId = state.nav.view === 'session'
        ? Number(state.nav.id || 0)
        : Number(state.activeProjectSessionId || 0);
      for (let i = runs.length - 1; i >= 0; i -= 1) {
        const run = runs[i];
        if (run && Number(run.session_id) === sessionId && run.status === 'running') return run;
      }
      return null;
    });

    const cp = {
      state,
      get currentProject() { return currentProject.value; },
      get currentJob() { return currentJob.value; },
      get metrics() { return metrics.value; },
      get projectTasks() { return projectTasks.value; },
      get projectSessions() { return projectSessions.value; },
      get projectJobs() { return projectJobs.value; },
      get activeProjectSessionId() { return state.activeProjectSessionId; },

      setNav, toggleExpanded, isExpanded,
      selectProject, toggleProject, toggleCategory, selectCategory,
      selectSession, selectEmbeddedSession, openSessionPage, selectTask, selectJob,
      toggleAuto, toggleDark,

      loadDashboard, loadTaskDetail, loadTaskLog, loadSessions, loadSessionChat, loadDaemonHealth, loadAIStatus,

      taskAction,
      taskBatchAction,
      toggleProjectForm, submitProject, deleteProject, renameProject,
      projectService, jobAction,
      runInspectWorkflow, workflowAction, workflowAutoAction,
      newSession, sendChat, sendEmbeddedChat, stopSessionRun, deleteSession,

      isTaskPending: (taskId) => !!state.pendingTasks[taskId],
      taskPendingAction: (taskId) => state.pendingTasks[taskId] || '',
      isActionPending: (actionKey) => isActionPending(actionKey),
      ACTION_KEYS,
      pushToast, dismissToast,
      confirm: confirmDialog,
      resolveConfirm,
      registerChatScroll,
      liveEventsForJob, liveEventsForTask,
      sessionRunForMessage,
      get activeSessionRun() { return activeSessionRun.value; },
    };

    CP.app = cp;
    provide('cp', cp);
    return { cp };
  }

  return Object.freeze({ setup });
})();
