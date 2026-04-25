/* App-level state/actions boundary extracted from app.js.
 * Keeps state transitions and side-effects separate from root rendering. */
/* global Vue, CP */

window.CP = window.CP || {};
CP.AppStateBoundary = CP.AppStateBoundary || (() => {
  const { reactive, computed, onMounted, onUnmounted, nextTick, provide, watch } = Vue;

  function setup() {
    /* ── Reactive state ──────────────────────────────── */
    const state = reactive({
      /* data from server. `tasks` / `jobs` always reflect the currently
       * selected project and live as a reactive alias on top of
       * `tasksByProject[nav.project]` / `jobsByProject[nav.project]`. The
       * per-project maps exist so the sidebar can render each project's
       * tree without the "wrong project's tasks briefly show under the
       * newly selected one" race we had before (caused by updating
       * `state.tasks` only AFTER the dashboard fetch completed). */
      projects: [], tasks: [], jobs: [], events: [], sessions: [],
      tasksByProject: {}, jobsByProject: {},

      /* navigation: {project, view, id} */
      nav: { project: null, view: 'overview', id: null },
      expanded: {},  /* tree-node key → boolean */

      /* detail caches keyed by navigation */
      taskDetail: null,
      taskDetailLoading: false,
      taskDetailError: '',
      sessionDetail: null,
      sessionMessages: [],

      /* Incremental task-log buffer: frontend owns the full text, backend
       * ships only the delta from `nextOffset` each tick. Reset whenever
       * navigation switches to a different task. */
      taskLog: { taskId: null, text: '', nextOffset: 0, size: 0, done: true, loading: false },

      /* global UI */
      autoRefresh: true, timer: null,
      loading: false, sending: false, newSessionLoading: false,
      /* Action protocol: frontend keeps per-action pending flags keyed by
       * stable action names so each view can subscribe only to the action it
       * triggers (instead of one global `sending` lock). */
      actionPending: {},
      projectSubmitting: false, deletingProject: '',
      servicePending: '',
      /* Map of taskId → pending action name (retry / stop / promote / split /
       * cancel / archive / delete).
       * Used by per-row buttons to show their own spinner without freezing
       * the rest of the page. */
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

      /* live progress events from SSE — ring buffer, most-recent last */
      liveEvents: [],

      /* Local daemon health — pushed over SSE (see openEventStream).
       * A single HTTP fallback on mount seeds the state before the SSE
       * priming event lands; after that it updates in near-real-time. */
      daemonHealth: { alive: true, running: false, pid: 0, reason: '', stale_seconds: 0 },

      /* forms */
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
      /* active clarification draft per session.
       * sessionId -> {questions, answers, original_title?, qa_history?} */
      clarifyDrafts: {},
    });

    let chatScrollEl = null;
    const registerChatScroll = (el) => { chatScrollEl = el; };

    /* ── Toast helpers ───────────────────────────────── *
     * Identical (message, type) toasts are coalesced: instead of stacking N
     * duplicate cards on failed-poll storms, we bump a `count` on the
     * existing toast. Non-error toasts also reset their auto-dismiss timer so
     * the merged toast stays visible long enough for the user to notice it
     * was updated. */
    const _toastTimers = new Map();
    const ACTION_KEYS = Object.freeze({
      GOAL_SUBMIT: 'goal.submit',
      COMPOSER_SUBMIT: 'composer.submit',
      TASK_BATCH_IMPORT: 'tasks.import',
      SESSION_SEND: 'session.send',
      SESSION_DELETE: 'session.delete',
      SESSION_CLARIFY_REPLY: 'session.clarify.reply',
      SESSION_CLARIFY_CANCEL: 'session.clarify.cancel',
    });
    const _REFRESH_BLOCKING_ACTIONS = new Set([
      ACTION_KEYS.GOAL_SUBMIT,
      ACTION_KEYS.COMPOSER_SUBMIT,
      ACTION_KEYS.TASK_BATCH_IMPORT,
      ACTION_KEYS.SESSION_SEND,
      ACTION_KEYS.SESSION_DELETE,
      ACTION_KEYS.SESSION_CLARIFY_REPLY,
      ACTION_KEYS.SESSION_CLARIFY_CANCEL,
    ]);
    function _syncLegacySending() {
      /* Backward-compatible aggregate flag retained for existing call sites. */
      state.sending = Object.values(state.actionPending || {}).some(Boolean);
    }
    function _isActionPending(actionKey) {
      if (!actionKey) return false;
      return !!state.actionPending[actionKey];
    }
    function _setActionPending(actionKey, pending) {
      if (!actionKey) return;
      state.actionPending[actionKey] = !!pending;
      _syncLegacySending();
    }
    function _isRefreshBlocked() {
      for (const key of _REFRESH_BLOCKING_ACTIONS) {
        if (_isActionPending(key)) return true;
      }
      return false;
    }
    async function _runScopedAction(actionKey, runner) {
      if (_isActionPending(actionKey)) return false;
      _setActionPending(actionKey, true);
      try {
        await runner();
        return true;
      } finally {
        _setActionPending(actionKey, false);
      }
    }

    function pushToast(message, type = 'info') {
      const existing = state.toasts.find(t => t.message === message && t.type === type);
      if (existing) {
        existing.count = (existing.count || 1) + 1;
        if (type !== 'error') {
          clearTimeout(_toastTimers.get(existing.id));
          _toastTimers.set(existing.id, setTimeout(() => dismissToast(existing.id), 4000));
        }
        return;
      }
      const id = ++state.toastSeq;
      state.toasts.push({ id, message, type, count: 1 });
      if (type !== 'error') {
        _toastTimers.set(id, setTimeout(() => dismissToast(id), 4000));
      }
    }
    function dismissToast(id) {
      const timer = _toastTimers.get(id);
      if (timer) { clearTimeout(timer); _toastTimers.delete(id); }
      state.toasts = state.toasts.filter(t => t.id !== id);
    }

    let _confirmResolver = null;
    function _resolveConfirm(result) {
      const resolver = _confirmResolver;
      _confirmResolver = null;
      state.confirmDialog.open = false;
      if (resolver) resolver(!!result);
    }
    function confirmDialog(options = {}) {
      const opts = options || {};
      return new Promise((resolve) => {
        if (_confirmResolver) _resolveConfirm(false);
        state.confirmDialog.title = opts.title || '请确认';
        state.confirmDialog.message = opts.message || '';
        state.confirmDialog.confirmText = opts.confirmText || '确认';
        state.confirmDialog.cancelText = opts.cancelText || '取消';
        state.confirmDialog.tone = opts.tone || 'danger';
        state.confirmDialog.open = true;
        _confirmResolver = resolve;
      });
    }

    /* ── Computed ────────────────────────────────────── */
    const currentProject = computed(() =>
      state.projects.find(p => p.name === state.nav.project) || null
    );
    const metrics = computed(() => {
      const st = (currentProject.value && currentProject.value.stats) || {};
      return [
        { label: '进行中', value: st.in_progress || 0, tone: 'info' },
        { label: '待办', value: st.backlog || 0, tone: '' },
        { label: '失败 / 取消', value: (st.failed || 0) + (st.cancelled || 0), tone: 'danger' },
        { label: '已完成', value: st.done || 0, tone: 'success' },
        { label: '总任务', value: st.total || 0, tone: '' },
      ];
    });

    /* Tasks/sessions/jobs belonging to current project */
    const projectTasks = computed(() => state.tasks);
    const projectSessions = computed(() =>
      state.sessions.filter(sess => !state.nav.project || sess.project === state.nav.project)
    );
    const projectJobs = computed(() => state.jobs);

    const currentJob = computed(() => {
      if (state.nav.view !== 'job' || !state.nav.id) return null;
      return state.jobs.find(j => j.id === state.nav.id) || null;
    });

    /* ── Navigation ──────────────────────────────────── *
     * Nav state is mirrored to ``location.hash`` and ``localStorage`` so a
     * page refresh / reopen lands the user back on the same task / session
     * view (and keeps the 阶段日志摘要 / live log panes populated). Writing
     * both is belt-and-braces: the hash survives sharing links, and
     * localStorage handles the case where the user clears the URL. */
    const NAV_STORAGE_KEY = 'cp-nav-v1';
    const EXPAND_STORAGE_KEY = 'cp-expand-v1';
    let _navSyncing = false;  /* suppress recursion when hashchange triggers setNav */

    function _navToHash(n) {
      return CP.StateBoundary.navToHash(n);
    }
    function _navFromHash(hash) {
      return CP.StateBoundary.navFromHash(hash);
    }
    function _persistNav() {
      try { localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify(state.nav)); } catch (e) { /* ignore */ }
      const target = _navToHash(state.nav);
      if (target && target !== location.hash) {
        _navSyncing = true;
        try { history.replaceState(null, '', target); } finally { _navSyncing = false; }
      }
    }
    function _readStoredNav() {
      /* Prefer hash (shareable, survives tab reopen with the same URL);
       * fall back to localStorage for browsers that strip hashes. */
      const fromHash = _navFromHash(location.hash);
      if (fromHash) return fromHash;
      try {
        const raw = localStorage.getItem(NAV_STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && parsed.project) return parsed;
      } catch (e) { /* ignore */ }
      return null;
    }
    const CATEGORY_VIEWS = ['sessions', 'tasks', 'jobs'];
    const _categoryKey = (project, view) => CP.StateBoundary.categoryKey(project, view);
    function _openProjectCategory(project, view) {
      CP.StateBoundary.openProjectCategory(state, project, view, CATEGORY_VIEWS);
    }
    function _normalizeProjectCategoryExpanded(project) {
      CP.StateBoundary.normalizeProjectCategoryExpanded(state, project, CATEGORY_VIEWS);
    }
    function _viewToCategory(view) {
      return CP.StateBoundary.viewToCategory(view, CATEGORY_VIEWS);
    }
    function _applyRestoredNav(restored) {
      if (!restored || !restored.project) return;
      state.nav.project = restored.project;
      state.nav.view = restored.view || 'overview';
      state.nav.id = restored.id ?? null;
      state.expanded[restored.project] = true;
      const restoredCategory = _viewToCategory(restored.view);
      if (restoredCategory) {
        _openProjectCategory(restored.project, restoredCategory);
      }
      /* Kick the appropriate loaders so panes repopulate. */
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
      /* Pivot the current tasks/jobs aliases immediately when the project
       * changes. Without this, Sidebar's `projectTasks(newProject)` — which
       * reads `state.tasks` when the project matches nav — would briefly
       * show the OLD project's tasks labelled under the new project
       * (classic async race we call "串"). */
      if (partial.project !== undefined && partial.project !== prevProject) {
        CP.StateBoundary.pivotProjectAliases(state, partial.project);
      }
      _persistNav();
    }
    function toggleExpanded(key) {
      state.expanded[key] = !state.expanded[key];
    }
    function isExpanded(key, defaultOpen = false) {
      return state.expanded[key] === undefined ? defaultOpen : state.expanded[key];
    }

    function selectProject(name) {
      /* Clicking a project name is now a pure navigation operation —
       * tasks/jobs for every project were already hydrated on the
       * initial dashboard load, and setNav's pivot swaps state.tasks /
       * state.jobs to the newly selected project instantly. Any freshness
       * that's needed comes through the SSE-driven refresh path. */
      state.expanded[name] = true;
      setNav({ project: name, view: 'overview', id: null });
    }
    function toggleProject(name) {
      /* click on chevron — just toggle expand */
      state.expanded[name] = !state.expanded[name];
    }
    function toggleCategory(project, view) {
      state.expanded[project] = true;
      const key = _categoryKey(project, view);
      const willOpen = !isExpanded(key, false);
      if (willOpen) _openProjectCategory(project, view);
      else state.expanded[key] = false;
    }
    function selectCategory(project, view) {
      /* view ∈ 'sessions' | 'tasks' | 'jobs' */
      state.expanded[project] = true;
      _openProjectCategory(project, view);
      setNav({ project, view, id: null });
    }
    function selectSession(project, id) {
      state.expanded[project] = true;
      _openProjectCategory(project, 'sessions');
      setNav({ project, view: 'session', id });
      loadSessionChat();
    }
    function selectTask(project, id) {
      state.expanded[project] = true;
      _openProjectCategory(project, 'tasks');
      setNav({ project, view: 'task', id });
      loadTaskDetail();
      loadTaskLog(id, { reset: true });
    }
    function selectJob(project, id) {
      state.expanded[project] = true;
      _openProjectCategory(project, 'jobs');
      setNav({ project, view: 'job', id });
    }

    function toggleAuto() { state.autoRefresh = !state.autoRefresh; schedule(); }
    function toggleDark() {
      state.dark = !state.dark;
      document.documentElement.dataset.theme = state.dark ? 'dark' : 'light';
      try { localStorage.setItem('cp-dark', state.dark ? '1' : '0'); } catch (e) { /* ignore */ }
    }

    /* ── Data loading ────────────────────────────────── *
     * Every request that writes into ``state`` captures the current nav at
     * call time and bails out if the user has since switched context. This
     * prevents a slow response for project A from overwriting state after
     * the user jumped to project B (classic async race in dashboards). */
    let _dashboardReqSeq = 0;
    async function loadDashboard() {
      if (_isRefreshBlocked()) return;
      const reqId = ++_dashboardReqSeq;
      state.loading = true;
      try {
        /* One bulk fetch hydrates EVERY project's tasks/jobs. Switching
         * projects after this is a pure nav update — no network, no
         * loading flicker, no cross-project data leaks. */
        const data = await CP.api.get('/api/projects');
        if (reqId !== _dashboardReqSeq) return;
        state.projects = data.projects || [];
        state.events = data.events || [];
        state.tasksByProject = data.tasks_by_project || {};
        state.jobsByProject = data.jobs_by_project || {};
        /* Pivot the aliases to whichever project is currently selected. */
        const active = state.nav.project || data.selected_project || null;
        CP.StateBoundary.pivotProjectAliases(state, active);
        /* auto-pick first project on first load */
        if (!state.nav.project && data.selected_project) {
          state.nav.project = data.selected_project;
          state.expanded[data.selected_project] = true;
          _persistNav();
        }
        await loadSessions();
        if (reqId !== _dashboardReqSeq) return;
        /* if viewing a task/session that went away, fall back */
        if (state.nav.view === 'task' && state.nav.id) {
          if (!state.tasks.find(t => t.id === state.nav.id)) {
            setNav({ view: 'overview', id: null });
          } else {
            await loadTaskDetail({ silent: true });
          }
        }
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        if (reqId === _dashboardReqSeq) {
          state.loading = false;
        }
      }
    }

    let _taskDetailBoundary = null;
    function _ensureTaskDetailBoundary() {
      if (_taskDetailBoundary) return _taskDetailBoundary;
      _taskDetailBoundary = CP.createTaskDetailBoundary({
        state,
        pushToast,
        loadDashboard,
      });
      return _taskDetailBoundary;
    }
    async function loadTaskDetail(options = {}) {
      return _ensureTaskDetailBoundary().loadTaskDetail(options);
    }
    function _handleTaskLogStreamEvent(event) {
      return _ensureTaskDetailBoundary().handleTaskLogStreamEvent(event);
    }
    async function loadTaskLog(taskId, options = {}) {
      return _ensureTaskDetailBoundary().loadTaskLog(taskId, options);
    }

    async function loadDaemonHealth() {
      try {
        const qs = state.nav.project ? `?project=${encodeURIComponent(state.nav.project)}` : '';
        const data = await CP.api.get(`/api/daemon/health${qs}`);
        state.daemonHealth = data || state.daemonHealth;
      } catch (_e) { /* silent — network errors already toasted elsewhere */ }
    }

    async function loadSessions() {
      /* Always fetch every project's sessions: the sidebar needs the full
       * list to show per-project counts and expanded session entries
       * regardless of which project is currently selected. */
      try {
        const data = await CP.api.get('/api/sessions');
        state.sessions = data.sessions || [];
      } catch (err) { /* silent */ }
    }

    async function loadSessionChat() {
      if (state.nav.view !== 'session' || !state.nav.id) return;
      const targetId = state.nav.id;
      try {
        const data = await CP.api.get(`/api/sessions/${targetId}`);
        /* Guard: user may have switched to another session while we waited. */
        if (state.nav.view !== 'session' || state.nav.id !== targetId) return;
        state.sessionDetail = data.session;
        state.sessionMessages = data.messages || [];
        _syncSessionClarifyDraft(targetId, state.sessionMessages);
        await nextTick();
        if (chatScrollEl) chatScrollEl.scrollTop = chatScrollEl.scrollHeight;
      } catch (err) {
        pushToast(err.message, 'error');
      }
    }

    /* ── Actions ─────────────────────────────────────── */
    async function taskAction(taskId, action) {
      return _ensureTaskDetailBoundary().taskAction(taskId, action);
    }
    async function taskBatchAction(taskIds, action) {
      return _ensureTaskDetailBoundary().taskBatchAction(taskIds, action);
    }

    async function submitGoal() {
      if (!state.nav.project) { pushToast('先选择一个项目', 'error'); return; }
      const goalClarify = state.goalClarify;
      const clarifyAnswers = goalClarify
        ? CP.exportClarifyAnswers(goalClarify.questions, goalClarify.answers)
        : [];
      const text = state.goalText.trim();
      if (!text && !clarifyAnswers.length) { pushToast('输入不能为空', 'error'); return; }
      await _runScopedAction(ACTION_KEYS.GOAL_SUBMIT, async () => {
        state.answer = null;
        try {
          const payload = {
            project: state.nav.project, text, category: state.goalCategory,
          };
          if (goalClarify) {
            payload.original_title = goalClarify.original_title;
            payload.qa_history = goalClarify.qa_history || [];
            payload.clarify_answers = clarifyAnswers;
            payload.clarify_questions = goalClarify.questions || [];
          }
          const out = await CP.api.post('/api/goal', payload);
          if (out.intent === 'question' || out.intent === 'command') {
            state.answer = out.message || '完成';
            state.goalClarify = null;
          } else if (out.intent === 'clarify') {
            state.goalClarify = _buildClarifyStateFromPayload(
              out,
              {
                fallbackTitle: (goalClarify && goalClarify.original_title) || text,
                existing: goalClarify,
              },
            );
            state.answer = _renderClarifyMessage(
              out.message || '需要补充信息',
              state.goalClarify ? state.goalClarify.questions : [],
            );
            state.goalText = '';
            return;
          } else {
            state.goalClarify = null;
            pushToast(out.message || '提交成功', 'success');
            await loadDashboard();
          }
          state.goalText = '';
        } catch (err) {
          pushToast(err.message, 'error');
        }
      });
    }

    async function submitComposer() {
      if (!state.nav.project) { pushToast('先选择一个项目', 'error'); return; }
      const composerClarify = state.composerMode === 'requirement' ? state.composerClarify : null;
      const clarifyAnswers = composerClarify
        ? CP.exportClarifyAnswers(composerClarify.questions, composerClarify.answers)
        : [];
      const title = state.composer.title.trim();
      if (!title && state.composerMode !== 'requirement') { pushToast('标题不能为空', 'error'); return; }
      if (!title && !clarifyAnswers.length && state.composerMode === 'requirement') {
        pushToast('标题不能为空', 'error'); return;
      }
      await _runScopedAction(ACTION_KEYS.COMPOSER_SUBMIT, async () => {
        const payload = {
          project: state.nav.project, title,
          content: state.composer.content,
          priority: state.composer.priority,
          agent: state.composer.agent,
          planner: state.composer.planner,
          execute: state.composer.execute,
        };
        if (composerClarify) {
          payload.original_title = composerClarify.original_title;
          payload.qa_history = composerClarify.qa_history || [];
          payload.clarify_answers = clarifyAnswers;
          payload.clarify_questions = composerClarify.questions || [];
        }
        try {
          let out;
          if (state.composerMode === 'task' || state.composerMode === 'task_ai') {
            // task → mode=full（用户自己写完整 content）
            // task_ai → mode=ai_complete（后端调 AI 补 content）
            payload.mode = state.composerMode === 'task_ai' ? 'ai_complete' : 'full';
            if (state.composerMode === 'task_ai') {
              // mode=ai_complete 不需要前端传 content；服务端会拒空 content
              // 时同样要求模板合规，所以这里清空避免误传脏数据。
              payload.content = '';
            }
            out = await CP.api.post('/api/tasks', payload);
            if (out.task) selectTask(state.nav.project, out.task.id);
            state.composer.content = '';
          } else {
            out = await CP.api.post('/api/requirements', payload);
            if (out.intent === 'clarify') {
              state.composerClarify = _buildClarifyStateFromPayload(
                out,
                {
                  fallbackTitle: (composerClarify && composerClarify.original_title) || title,
                  existing: composerClarify,
                },
              );
              state.answer = _renderClarifyMessage(
                out.message || '需要补充信息',
                state.composerClarify ? state.composerClarify.questions : [],
              );
              state.composer.title = '';
              pushToast('需要补充信息', 'warning');
              return;
            }
            state.composerClarify = null;
          }
          state.composer.title = '';
          pushToast(out.message || '提交成功', 'success');
          await loadDashboard();
        } catch (err) {
          pushToast(err.message, 'error');
        }
      });
    }

    async function loadTaskTemplateSchema({ force = false } = {}) {
      if (state.taskTemplateLoading) return state.taskTemplateSchema;
      if (!force && state.taskTemplateSchema) return state.taskTemplateSchema;
      state.taskTemplateLoading = true;
      state.taskTemplateError = '';
      try {
        const out = await CP.api.get('/api/task-template');
        state.taskTemplateSchema = out.schema || null;
        return state.taskTemplateSchema;
      } catch (err) {
        state.taskTemplateError = err.message || '模板 schema 加载失败';
        return null;
      } finally {
        state.taskTemplateLoading = false;
      }
    }

    async function submitTaskBatch(validation = null) {
      if (!state.nav.project) { pushToast('先选择一个项目', 'error'); return; }
      const raw = String((state.batchComposer && state.batchComposer.raw) || '').trim();
      if (!raw) { pushToast('先粘贴批量任务 JSON', 'error'); return; }

      const resolvedValidation = validation || CP.validateTaskBatchImport(raw, state.taskTemplateSchema);
      if (!resolvedValidation.valid) {
        const firstInvalidItem = (resolvedValidation.items || []).find(item => !item.ok && item.errors && item.errors.length);
        const firstError = resolvedValidation.parseError
          || (resolvedValidation.globalErrors && resolvedValidation.globalErrors[0])
          || (firstInvalidItem && firstInvalidItem.errors && firstInvalidItem.errors[0])
          || '当前批量任务不符合模板 schema。';
        pushToast(firstError, 'error');
        return;
      }

      await _runScopedAction(ACTION_KEYS.TASK_BATCH_IMPORT, async () => {
        try {
          const out = await CP.api.post('/api/tasks/import', {
            project: state.nav.project,
            items: resolvedValidation.items.map(item => item.raw),
          });
          state.batchComposer.raw = '';
          pushToast(out.message || '批量导入成功', 'success');
          await loadDashboard();
          if (out.tasks && out.tasks.length) selectTask(state.nav.project, out.tasks[0].id);
        } catch (err) {
          pushToast(err.message, 'error');
        }
      });
    }

    async function newSession() {
      if (!state.nav.project) { pushToast('先选择一个项目', 'error'); return; }
      state.newSessionLoading = true;
      try {
        const out = await CP.api.post('/api/sessions', { project: state.nav.project, title: '' });
        await loadSessions();
        selectSession(state.nav.project, out.session.id);
        pushToast('会话已创建', 'success');
      } catch (err) {
        pushToast(err.message, 'error');
      } finally { state.newSessionLoading = false; }
    }

    function toggleProjectForm(open = null) {
      state.projectForm.open = open == null ? !state.projectForm.open : !!open;
    }

    async function submitProject() {
      const path = state.projectForm.path.trim();
      if (!path) { pushToast('工作目录不能为空', 'error'); return; }
      state.projectSubmitting = true;
      try {
        const out = await CP.api.post('/api/projects', {
          path,
          name: state.projectForm.name.trim(),
          no_config: !!state.projectForm.noConfig,
        });
        state.projectForm = { open: false, path: '', name: '', noConfig: false };
        await loadDashboard();
        if (out.project && out.project.name) selectProject(out.project.name);
        pushToast(out.message || '项目已注册', 'success');
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.projectSubmitting = false;
      }
    }

    async function deleteProject(name) {
      if (!name) return;
      const ok = await confirmDialog({
        title: '删除项目',
        message: `确定要删除项目 "${name}" 吗？\n工作目录不会被删除。`,
        confirmText: '删除',
        cancelText: '取消',
        tone: 'danger',
      });
      if (!ok) return;
      state.deletingProject = name;
      try {
        const out = await CP.api.del(`/api/projects/${encodeURIComponent(name)}`);
        if (state.nav.project === name) {
          setNav({ project: null, view: 'overview', id: null });
          state.taskDetail = null;
          state.sessionDetail = null;
          state.sessionMessages = [];
        }
        await loadDashboard();
        pushToast(out.message || '项目已删除', 'success');
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.deletingProject = '';
      }
    }

    async function projectService(service, action) {
      if (!state.nav.project) { pushToast('先选择一个项目', 'error'); return; }
      const key = `${service}:${action}`;
      state.servicePending = key;
      try {
        const out = await CP.api.post(
          `/api/projects/${encodeURIComponent(state.nav.project)}/${service}/${action}`,
          {},
        );
        pushToast(out.message || '操作完成', 'success');
        await loadDashboard();
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.servicePending = '';
      }
    }

    async function sendChat() {
      if (state.nav.view !== 'session' || !state.nav.id) return;
      if (state.clarifyDrafts[state.nav.id]) {
        pushToast('当前会话正在等待澄清回答，请先提交或取消。', 'warning');
        return;
      }
      const text = state.chatText.trim();
      if (!text) return;
      await _runScopedAction(ACTION_KEYS.SESSION_SEND, async () => {
        try {
          await CP.api.post(`/api/sessions/${state.nav.id}/messages`, {
            text, category: state.chatCategory,
          });
          state.chatText = '';
          await loadSessionChat();
          await loadSessions();
        } catch (err) {
          pushToast(err.message, 'error');
        }
      });
    }

    function cancelGoalClarify() {
      state.goalClarify = null;
      state.goalText = '';
      state.answer = null;
      pushToast('已取消当前这次需求规划', 'info');
    }

    function cancelComposerClarify() {
      state.composerClarify = null;
      state.composer.title = '';
      state.answer = null;
      pushToast('已取消当前这次需求规划', 'info');
    }

    async function cancelSessionClarify(sessionId) {
      if (!sessionId) return;
      await _runScopedAction(ACTION_KEYS.SESSION_CLARIFY_CANCEL, async () => {
        try {
          await CP.api.post(`/api/sessions/${sessionId}/messages`, {
            text: '/clear',
            category: 'auto',
          });
          delete state.clarifyDrafts[sessionId];
          if (state.nav.view === 'session' && state.nav.id === sessionId) {
            await loadSessionChat();
          }
          await loadSessions();
          pushToast('已取消当前这次需求规划', 'info');
        } catch (err) {
          pushToast(err.message, 'error');
        }
      });
    }

    async function deleteSession() {
      if (state.nav.view !== 'session' || !state.nav.id) return;
      const ok = await confirmDialog({
        title: '删除会话',
        message: '确定要删除这个会话吗？',
        confirmText: '删除',
        cancelText: '取消',
        tone: 'danger',
      });
      if (!ok) return;
      const sid = state.nav.id;
      await _runScopedAction(ACTION_KEYS.SESSION_DELETE, async () => {
        try {
          await CP.api.del(`/api/sessions/${sid}`);
          setNav({ view: 'sessions', id: null });
          state.sessionDetail = null;
          state.sessionMessages = [];
          await loadSessions();
          pushToast('会话已删除', 'success');
        } catch (err) {
          pushToast(err.message, 'error');
        }
      });
    }

    /* ── Refresh scheduling ──────────────────────────── *
     * Live updates are push-driven via SSE (see openEventStream below): any
     * event calls :func:`scheduleRefresh` which debounces a dashboard /
     * session / task-log reload into a single tick. The periodic timer
     * below is just a safety net for when SSE drops or the server emits
     * nothing despite state changing (e.g. rare sqlite concurrency paths).
     * It runs every :data:`FALLBACK_REFRESH_MS`, far less often than the
     * old 3s loop so the UI doesn't flicker / thrash during quiet periods.
     */
    const FALLBACK_REFRESH_MS = 30000;
    const REFRESH_DEBOUNCE_MS = 250;
    let _refreshTimer = 0;
    function scheduleRefresh({ immediate = false } = {}) {
      if (immediate) {
        if (_refreshTimer) { clearTimeout(_refreshTimer); _refreshTimer = 0; }
        _runRefresh();
        return;
      }
      if (_refreshTimer) return;
      _refreshTimer = setTimeout(() => {
        _refreshTimer = 0;
        _runRefresh();
      }, REFRESH_DEBOUNCE_MS);
    }
    function _runRefresh() {
      if (_isRefreshBlocked()) return;
      loadDashboard();
      if (state.nav.view === 'session' && state.nav.id) loadSessionChat();
      /* loadTaskLog is kicked directly from the SSE handler with the exact
       * task_id so switching tasks doesn't re-stream the previous one. */
    }
    function schedule() {
      clearInterval(state.timer);
      if (!state.autoRefresh) return;
      state.timer = setInterval(() => scheduleRefresh(), FALLBACK_REFRESH_MS);
    }

    function _messageMetadata(message) {
      if (!message || !message.metadata) return {};
      if (typeof message.metadata === 'object') return message.metadata;
      if (typeof message.metadata === 'string') {
        try { return JSON.parse(message.metadata); } catch (e) { return {}; }
      }
      return {};
    }
    function _legacyClarifyQuestionsFromMessage(message) {
      const content = String((message && message.content) || '');
      const questions = [];
      for (const line of content.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const stripped = line.trimStart();
        if (stripped.length !== line.length && /^(?:\d+[\.\)、:：]|[-*])\s+/.test(stripped)) continue;
        const match = /^\s*(?:(?:问题\s*)?\d+[\.\)、:：]|[一二三四五六七八九十]+[、.．:：]|[-*])\s*(.+)$/.exec(line);
        if (!match) continue;
        const text = CP._clarifyText(match[1]);
        if (!text) continue;
        questions.push({
          id: `legacy_q${questions.length + 1}`,
          type: 'text',
          text,
          options: [],
          allow_free_text: false,
        });
      }
      if (questions.length) return questions;
      const fallback = CP._clarifyText(content);
      if (fallback && !content.includes('\n')) {
        return [{
          id: 'legacy_q1',
          type: 'text',
          text: fallback,
          options: [],
          allow_free_text: false,
        }];
      }
      return [];
    }
    function _buildClarifyStateFromPayload(payload, options = {}) {
      const fallbackTitle = CP._clarifyText(options.fallbackTitle);
      const existing = options.existing && typeof options.existing === 'object' ? options.existing : null;
      const questions = CP.normalizeClarifyQuestions(payload && payload.questions);
      if (!questions.length) return null;
      const qaHistory = Array.isArray(payload && payload.qa_history)
        ? payload.qa_history.slice()
        : Array.isArray(existing && existing.qa_history)
          ? existing.qa_history.slice()
          : [];
      return {
        original_title: CP._clarifyText(
          (payload && payload.original_title)
          || (existing && existing.original_title)
          || fallbackTitle,
        ),
        qa_history: qaHistory,
        questions,
        answers: CP.createClarifyAnswerState(questions, existing && existing.answers),
      };
    }
    function _renderClarifyMessage(message, questions) {
      const base = CP._clarifyText(message) || '为了更好地规划，请先回答几个问题。';
      const details = CP.clarifyQuestionsText(questions);
      return details ? `${base}\n\n${details}` : base;
    }
    function _syncSessionClarifyDraft(sessionId, messages) {
      if (!sessionId) return;
      const all = Array.isArray(messages) ? messages : [];
      let pendingMessage = null;
      for (let i = all.length - 1; i >= 0; i--) {
        const msg = all[i];
        if (msg && msg.role === 'assistant') {
          pendingMessage = msg.intent === 'clarify' ? msg : null;
          break;
        }
      }
      if (!pendingMessage) {
        delete state.clarifyDrafts[sessionId];
        return;
      }
      const metadata = _messageMetadata(pendingMessage);
      const structuredQuestions = CP.normalizeClarifyQuestions(metadata.questions || []);
      const nextDraft = _buildClarifyStateFromPayload(
        {
          questions: structuredQuestions.length
            ? structuredQuestions
            : _legacyClarifyQuestionsFromMessage(pendingMessage),
        },
        { existing: state.clarifyDrafts[sessionId] || null },
      );
      if (nextDraft) state.clarifyDrafts[sessionId] = nextDraft;
      else delete state.clarifyDrafts[sessionId];
    }

    /* ── SSE live progress stream ────────────────────── *
     * Keep the full event history (up to LIVE_EVENTS_MAX) so the user can
     * scroll back through a long-running job's output. CSS
     * `content-visibility: auto` on `.live-row` lets the browser skip
     * off-screen rows, so a 10k-deep buffer still renders smoothly. */
    const LIVE_EVENTS_MAX = 10000;
    let sseHandle = null;
    function openEventStream() {
      if (sseHandle) return;
      const qs = state.nav.project ? `?project=${encodeURIComponent(state.nav.project)}` : '';
      sseHandle = CP.sse.open(`/api/events/stream${qs}`, (event) => {
        if (_handleTaskLogStreamEvent(event)) return;
        /* Daemon-health events are piggy-backed on the progress stream
         * by the backend (see webui.py::_stream_progress_events). They
         * carry the full health payload in event.extra and only get
         * pushed when the state actually changed, so updating the
         * banner is near-instant (~2s) and doesn't need a separate
         * polling timer. */
        if (event && event.stage === 'daemon-health' && event.extra) {
          state.daemonHealth = event.extra;
          return;  /* not a progress event — don't pollute liveEvents */
        }
        state.liveEvents.push(event);
        if (state.liveEvents.length > LIVE_EVENTS_MAX) {
          state.liveEvents.splice(0, state.liveEvents.length - LIVE_EVENTS_MAX);
        }
        /* Progress event → debounced dashboard/session refresh. This is
         * what replaces the old 3s polling: the UI updates in near-real
         * time, and bursts of events collapse into a single state reload. */
        scheduleRefresh();
        /* Live log delta — fetch immediately for the task we're viewing so
         * the terminal pane feels streamed rather than tick-based. Events
         * without a task_id (global planner / recon hooks) also qualify
         * because they typically precede builder writes. */
        const tid = state.nav.view === 'task' ? state.nav.id : null;
        if (tid && (event.task_id === tid || event.task_id == null)) {
          loadTaskLog(tid);
        }
      });
    }
    function closeEventStream() {
      if (sseHandle) { sseHandle.close(); sseHandle = null; }
    }

    let _mounted = false;
    watch(() => state.nav.project, (nextProject, prevProject) => {
      if (nextProject === prevProject) return;
      loadDaemonHealth();
      if (!_mounted) return;
      closeEventStream();
      openEventStream();
    });

    /* ── Keyboard shortcuts ──────────────────────────── */
    function installKeyboardShortcuts() {
      document.addEventListener('keydown', (e) => {
        /* Ignore when typing in an input / textarea / contentEditable. */
        const tag = (e.target && e.target.tagName) || '';
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || (e.target && e.target.isContentEditable)) return;
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        /* `?` — open a simple toast with the cheatsheet. */
        if (e.key === '?') {
          pushToast('快捷键: d=切换深色 · r=手动刷新 · n=新建会话 · /=聚焦输入', 'info');
          e.preventDefault(); return;
        }
        if (e.key === 'd') { toggleDark(); e.preventDefault(); return; }
        if (e.key === 'r') { loadDashboard(); e.preventDefault(); return; }
        if (e.key === 'n') { newSession(); e.preventDefault(); return; }
        if (e.key === '/') {
          const inp = document.querySelector('.chat-input-bar input, .goal-input input, input[placeholder]');
          if (inp) { inp.focus(); e.preventDefault(); }
        }
      });
    }

    /* Hashchange listener keeps browser back/forward in sync with state.nav.
     * The `_navSyncing` flag suppresses feedback when setNav itself wrote
     * the hash via history.replaceState. */
    function _onHashChange() {
      if (_navSyncing) return;
      const parsed = _navFromHash(location.hash);
      if (!parsed) return;
      if (parsed.project === state.nav.project
        && parsed.view === state.nav.view
        && parsed.id === state.nav.id) return;
      _applyRestoredNav(parsed);
    }

    onMounted(() => {
      _mounted = true;
      try { state.dark = localStorage.getItem('cp-dark') === '1'; } catch (e) { /* ignore */ }
      document.documentElement.dataset.theme = state.dark ? 'dark' : 'light';
      /* Restore sidebar expanded state BEFORE the first render so the
       * tree paints with the same nodes open / closed as before. */
      try {
        const raw = localStorage.getItem(EXPAND_STORAGE_KEY);
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === 'object') {
            Object.assign(state.expanded, parsed);
          }
        }
      } catch (e) { /* ignore */ }
      /* Keep localStorage in sync with every future mutation (toggle
       * chevrons, selectTask auto-expansions, etc.). Debounced via a
       * trailing rAF so we don't thrash on rapid toggles. */
      let _expandSaveQueued = false;
      watch(() => state.expanded, () => {
        if (_expandSaveQueued) return;
        _expandSaveQueued = true;
        requestAnimationFrame(() => {
          _expandSaveQueued = false;
          try {
            localStorage.setItem(EXPAND_STORAGE_KEY, JSON.stringify(state.expanded));
          } catch (e) { /* ignore */ }
        });
      }, { deep: true });
      /* Restore prior nav (hash first, then localStorage) BEFORE the first
       * dashboard load so loadDashboard sees the intended project. */
      const restored = _readStoredNav();
      if (restored && restored.project) {
        state.nav.project = restored.project;
        state.nav.view = restored.view || 'overview';
        state.nav.id = restored.id ?? null;
        state.expanded[restored.project] = true;
        const restoredCategory = _viewToCategory(state.nav.view);
        if (restoredCategory) _openProjectCategory(restored.project, restoredCategory);
      }
      loadDashboard().then(() => {
        for (const project of state.projects || []) {
          _normalizeProjectCategoryExpanded(project && project.name);
        }
        /* After the dashboard populates state.tasks/sessions/jobs, kick the
         * detail loaders so task-log / chat panes repopulate. */
        if (state.nav.view === 'task' && state.nav.id) {
          loadTaskDetail();
          loadTaskLog(state.nav.id, { reset: true });
        } else if (state.nav.view === 'session' && state.nav.id) {
          loadSessionChat();
        }
      });
      /* Daemon health is pushed via SSE now (see openEventStream). We
       * still do a single HTTP GET on mount as a fallback for the small
       * window before the SSE connection has opened and delivered its
       * priming event. After that, state.daemonHealth stays fresh in
       * near-real-time without any polling. */
      loadDaemonHealth();
      schedule();
      openEventStream();
      installKeyboardShortcuts();
      window.addEventListener('hashchange', _onHashChange);
    });
    onUnmounted(() => {
      _mounted = false;
      clearInterval(state.timer);
      closeEventStream();
      window.removeEventListener('hashchange', _onHashChange);
    });

    /* ── Provided to all descendants ──────────────────
     * Computed refs are NOT auto-unwrapped when accessed via `cp.xxx`,
     * so we expose them through getters that read `.value` internally. */
    /* ── Live-events helpers for detail panes ────────── */
    const liveEventsForJob = (_jobId) => {
      /* Events aren't labelled by job ID today; we show all events emitted
       * after the job started as an approximation. Future: correlate via
       * task_ids. The argument is intentionally ignored for now but kept
       * so call sites are explicit about the scope they want. */
      return state.liveEvents.slice();
    };
    const liveEventsForTask = (taskId) => {
      if (!taskId) return [];
      return state.liveEvents.filter(ev => ev && (ev.task_id === taskId || ev.task_id == null));
    };

    /* ── Clarification quick-reply ───────────────────── */
    async function submitClarifyAnswer(sessionId, answerText = '') {
      const draft = state.clarifyDrafts[sessionId] || null;
      const clarifyAnswers = draft
        ? CP.exportClarifyAnswers(draft.questions, draft.answers)
        : [];
      const text = String(answerText || '').trim();
      if (!text && !clarifyAnswers.length) return;
      await _runScopedAction(ACTION_KEYS.SESSION_CLARIFY_REPLY, async () => {
        try {
          const out = await CP.api.post(`/api/sessions/${sessionId}/messages`, {
            text,
            category: 'auto',
            clarify_answers: clarifyAnswers,
          });
          if (out.intent === 'clarify') {
            state.clarifyDrafts[sessionId] = _buildClarifyStateFromPayload(
              out,
              { existing: draft },
            );
          } else {
            delete state.clarifyDrafts[sessionId];
          }
          await loadSessionChat();
          await loadSessions();
        } catch (err) {
          pushToast(err.message, 'error');
        }
      });
    }

    const cp = {
      state,
      get currentProject() { return currentProject.value; },
      get currentJob() { return currentJob.value; },
      get metrics() { return metrics.value; },
      get projectTasks() { return projectTasks.value; },
      get projectSessions() { return projectSessions.value; },
      get projectJobs() { return projectJobs.value; },
      /* nav */
      setNav, toggleExpanded, isExpanded,
      selectProject, toggleProject, toggleCategory, selectCategory,
      selectSession, selectTask, selectJob,
      toggleAuto, toggleDark,
      /* data */
      loadDashboard, loadTaskDetail, loadTaskLog, loadSessions, loadSessionChat, loadDaemonHealth,
      loadTaskTemplateSchema,
      /* actions */
      taskAction, submitGoal, submitComposer, submitTaskBatch,
      taskBatchAction,
      toggleProjectForm, submitProject, deleteProject,
      projectService,
      newSession, sendChat, deleteSession,
      submitClarifyAnswer,
      cancelGoalClarify, cancelComposerClarify, cancelSessionClarify,
      /* Per-task pending helpers for per-row spinners. */
      isTaskPending: (taskId) => !!state.pendingTasks[taskId],
      taskPendingAction: (taskId) => state.pendingTasks[taskId] || '',
      isActionPending: (actionKey) => _isActionPending(actionKey),
      ACTION_KEYS,
      pushToast, dismissToast,
      confirm: confirmDialog,
      resolveConfirm: _resolveConfirm,
      registerChatScroll,
      /* live events */
      liveEventsForJob, liveEventsForTask,
    };
    CP.app = cp;  /* debugging / extensions */
    provide('cp', cp);

    return { cp };
  }

  return Object.freeze({ setup });
})();
