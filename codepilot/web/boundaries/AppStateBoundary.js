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

      nav: { project: null, view: 'overview', id: null },
      expanded: {},

      taskDetail: null,
      taskDetailLoading: false,
      taskDetailError: '',
      sessionDetail: null,
      sessionMessages: [],

      taskLog: { taskId: null, text: '', nextOffset: 0, size: 0, done: true, loading: false },

      autoRefresh: true, timer: null,
      loading: false, sending: false, newSessionLoading: false,
      actionPending: {},
      projectSubmitting: false, deletingProject: '',
      servicePending: '',
      pendingTasks: {},
      dark: false,
      toasts: [], toastSeq: 0,
      answer: null,
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

      goalText: '', goalCategory: 'auto', goalClarify: null,
      projectForm: { open: false, path: '', name: '', noConfig: false },
      composerMode: 'requirement',
      composer: { title: '', content: '', priority: 'P2', agent: 'auto', planner: 'codex', execute: true },
      composerClarify: null,
      taskTemplateSchema: null,
      taskTemplateLoading: false,
      taskTemplateError: '',
      batchComposer: { raw: '' },
      chatText: '', chatCategory: 'auto',
      clarifyDrafts: {},
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

    const clarifyBoundary = CP.createAppClarifyBoundary({ state, pushToast });
    const buildClarifyStateFromPayload = clarifyBoundary.buildClarifyStateFromPayload;
    const renderClarifyMessage = clarifyBoundary.renderClarifyMessage;
    const syncSessionClarifyDraft = clarifyBoundary.syncSessionClarifyDraft;
    const cancelGoalClarify = clarifyBoundary.cancelGoalClarify;
    const cancelComposerClarify = clarifyBoundary.cancelComposerClarify;

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
      state.nav.project = restored.project;
      state.nav.view = restored.view || 'overview';
      state.nav.id = restored.id ?? null;
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
      Object.assign(state.nav, partial);
      if (partial.project !== undefined && partial.project !== prevProject) {
        CP.StateBoundary.pivotProjectAliases(state, partial.project);
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
        state.jobsByProject = data.jobs_by_project || {};
        const active = state.nav.project || data.selected_project || null;
        CP.StateBoundary.pivotProjectAliases(state, active);
        if (!state.nav.project && data.selected_project) {
          state.nav.project = data.selected_project;
          state.expanded[data.selected_project] = true;
          persistNav();
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

    async function loadSessions() {
      try {
        const data = await CP.api.get('/api/sessions');
        state.sessions = data.sessions || [];
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
        selectSession,
        setNav,
        buildClarifyStateFromPayload,
        syncSessionClarifyDraft,
      });
      return sessionBoundary;
    }

    async function loadSessionChat() {
      return ensureSessionBoundary().loadSessionChat();
    }

    async function newSession() {
      return ensureSessionBoundary().newSession();
    }

    async function sendChat() {
      return ensureSessionBoundary().sendChat();
    }

    async function cancelSessionClarify(sessionId) {
      return ensureSessionBoundary().cancelSessionClarify(sessionId);
    }

    async function deleteSession() {
      return ensureSessionBoundary().deleteSession();
    }

    async function submitClarifyAnswer(sessionId, answerText = '') {
      return ensureSessionBoundary().submitClarifyAnswer(sessionId, answerText);
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
        selectTask,
        selectProject,
        setNav,
        confirmDialog,
        buildClarifyStateFromPayload,
        renderClarifyMessage,
      });
      return submissionBoundary;
    }

    async function submitGoal() {
      return ensureSubmissionBoundary().submitGoal();
    }

    async function submitComposer() {
      return ensureSubmissionBoundary().submitComposer();
    }

    async function loadTaskTemplateSchema(options = {}) {
      return ensureSubmissionBoundary().loadTaskTemplateSchema(options);
    }

    async function submitTaskBatch(validation = null) {
      return ensureSubmissionBoundary().submitTaskBatch(validation);
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

    async function projectService(service, action) {
      return ensureSubmissionBoundary().projectService(service, action);
    }

    function runRefresh() {
      if (isRefreshBlocked()) return;
      loadDashboard();
      if (state.nav.view === 'session' && state.nav.id) loadSessionChat();
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
        if (event && event.stage === 'daemon-health' && event.extra) {
          state.daemonHealth = event.extra;
          return;
        }
        state.liveEvents.push(event);
        if (state.liveEvents.length > LIVE_EVENTS_MAX) {
          state.liveEvents.splice(0, state.liveEvents.length - LIVE_EVENTS_MAX);
        }
        scheduleRefresh();
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

    watch(() => state.nav.project, (nextProject, prevProject) => {
      if (nextProject === prevProject) return;
      loadDaemonHealth();
      if (!mounted) return;
      closeEventStream();
      openEventStream();
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
          const input = document.querySelector('.chat-input-bar input, .goal-input input, input[placeholder]');
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
        state.nav.project = restored.project;
        state.nav.view = restored.view || 'overview';
        state.nav.id = restored.id ?? null;
        state.expanded[restored.project] = true;
        const restoredCategory = viewToCategory(state.nav.view);
        if (restoredCategory) openProjectCategory(restored.project, restoredCategory);
      }

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
      schedule();
      openEventStream();
      installKeyboardShortcuts();
      window.addEventListener('hashchange', onHashChange);
    });

    onUnmounted(() => {
      mounted = false;
      clearInterval(state.timer);
      closeEventStream();
      window.removeEventListener('hashchange', onHashChange);
    });

    const liveEventsForJob = (_jobId) => state.liveEvents.slice();

    const liveEventsForTask = (taskId) => {
      if (!taskId) return [];
      return state.liveEvents.filter((event) => event && (event.task_id === taskId || event.task_id == null));
    };

    const cp = {
      state,
      get currentProject() { return currentProject.value; },
      get currentJob() { return currentJob.value; },
      get metrics() { return metrics.value; },
      get projectTasks() { return projectTasks.value; },
      get projectSessions() { return projectSessions.value; },
      get projectJobs() { return projectJobs.value; },

      setNav, toggleExpanded, isExpanded,
      selectProject, toggleProject, toggleCategory, selectCategory,
      selectSession, selectTask, selectJob,
      toggleAuto, toggleDark,

      loadDashboard, loadTaskDetail, loadTaskLog, loadSessions, loadSessionChat, loadDaemonHealth,
      loadTaskTemplateSchema,

      taskAction, submitGoal, submitComposer, submitTaskBatch,
      taskBatchAction,
      toggleProjectForm, submitProject, deleteProject,
      projectService,
      newSession, sendChat, deleteSession,
      submitClarifyAnswer,
      cancelGoalClarify, cancelComposerClarify, cancelSessionClarify,

      isTaskPending: (taskId) => !!state.pendingTasks[taskId],
      taskPendingAction: (taskId) => state.pendingTasks[taskId] || '',
      isActionPending: (actionKey) => isActionPending(actionKey),
      ACTION_KEYS,
      pushToast, dismissToast,
      confirm: confirmDialog,
      resolveConfirm,
      registerChatScroll,
      liveEventsForJob, liveEventsForTask,
    };

    CP.app = cp;
    provide('cp', cp);
    return { cp };
  }

  return Object.freeze({ setup });
})();
