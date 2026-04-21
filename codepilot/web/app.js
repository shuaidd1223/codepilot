/* CodePilot main app — state, navigation, actions, root component. */
/* global Vue, CP */

const { createApp, reactive, computed, onMounted, onUnmounted, nextTick, ref, provide, watch } = Vue;

const RootApp = {
  setup() {
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
      projectSubmitting: false, deletingProject: '',
      servicePending: '',
      /* Map of taskId → pending action name (retry / stop / promote / split).
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
      goalText: '', goalCategory: 'auto',
      projectForm: { open: false, path: '', name: '', noConfig: false },
      composerMode: 'requirement',
      composer: { title: '', content: '', priority: 'P2', agent: 'auto', planner: 'codex', execute: true },
      chatText: '', chatCategory: 'auto',
      /* active clarification (intent=clarify) state per session. Map
       * sessionId -> {questions: [...], answerDraft: ''} */
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
      if (!n || !n.project) return '';
      const p = encodeURIComponent(n.project);
      if (n.view === 'task'    && n.id) return `#/p/${p}/task/${n.id}`;
      if (n.view === 'session' && n.id) return `#/p/${p}/session/${n.id}`;
      if (n.view === 'job'     && n.id) return `#/p/${p}/job/${n.id}`;
      if (n.view === 'tasks'    ) return `#/p/${p}/tasks`;
      if (n.view === 'sessions' ) return `#/p/${p}/sessions`;
      if (n.view === 'jobs'     ) return `#/p/${p}/jobs`;
      return `#/p/${p}`;
    }
    function _navFromHash(hash) {
      if (!hash || hash.length < 2) return null;
      const raw = hash.replace(/^#\/?/, '');
      const parts = raw.split('/').filter(Boolean);
      if (parts.length < 2 || parts[0] !== 'p') return null;
      const project = decodeURIComponent(parts[1]);
      if (!project) return null;
      if (parts.length === 2) return { project, view: 'overview', id: null };
      const view = parts[2];
      if (['task', 'session', 'job'].includes(view)) {
        const idNum = Number(parts[3]);
        if (!Number.isFinite(idNum)) return { project, view: 'overview', id: null };
        return { project, view, id: idNum };
      }
      if (['tasks', 'sessions', 'jobs', 'overview'].includes(view)) {
        return { project, view, id: null };
      }
      return { project, view: 'overview', id: null };
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
    const _categoryKey = (project, view) => `${project}/${view}`;
    function _openProjectCategory(project, view) {
      for (const v of CATEGORY_VIEWS) {
        state.expanded[_categoryKey(project, v)] = (v === view);
      }
    }
    function _normalizeProjectCategoryExpanded(project) {
      if (!project) return;
      const opened = CATEGORY_VIEWS.filter(v => !!state.expanded[_categoryKey(project, v)]);
      if (opened.length <= 1) return;
      _openProjectCategory(project, opened[0]);
    }
    function _viewToCategory(view) {
      if (!view) return null;
      if (CATEGORY_VIEWS.includes(view)) return view;
      if (view === 'session') return 'sessions';
      if (view === 'task') return 'tasks';
      if (view === 'job') return 'jobs';
      return null;
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
        const np = partial.project;
        state.tasks = (np && state.tasksByProject[np]) || [];
        state.jobs  = (np && state.jobsByProject[np])  || [];
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
    async function loadDashboard() {
      if (state.sending) return;
      state.loading = true;
      try {
        /* One bulk fetch hydrates EVERY project's tasks/jobs. Switching
         * projects after this is a pure nav update — no network, no
         * loading flicker, no cross-project data leaks. */
        const data = await CP.api.get('/api/projects');
        state.projects = data.projects || [];
        state.events = data.events || [];
        state.tasksByProject = data.tasks_by_project || {};
        state.jobsByProject = data.jobs_by_project || {};
        /* Pivot the aliases to whichever project is currently selected. */
        const active = state.nav.project || data.selected_project || null;
        state.tasks = (active && state.tasksByProject[active]) || [];
        state.jobs = (active && state.jobsByProject[active]) || [];
        /* auto-pick first project on first load */
        if (!state.nav.project && data.selected_project) {
          state.nav.project = data.selected_project;
          state.expanded[data.selected_project] = true;
          _persistNav();
        }
        await loadSessions();
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
        state.loading = false;
      }
    }

    async function loadTaskDetail({ silent = false } = {}) {
      if (state.nav.view !== 'task' || !state.nav.id) return;
      const targetId = state.nav.id;
      const hadCurrent = !!(state.taskDetail && state.taskDetail.id === targetId);
      if (!silent && !hadCurrent) state.taskDetailLoading = true;
      state.taskDetailError = '';
      const reqId = ++loadTaskDetail._reqSeq;
      try {
        const data = await CP.api.get(`/api/tasks/${targetId}`);
        if (reqId !== loadTaskDetail._reqSeq) return;
        if (state.nav.view === 'task' && state.nav.id === targetId) {
          const task = (data && typeof data === 'object' && data.id) ? data : null;
          state.taskDetail = task;
          state.taskDetailError = task ? '' : '暂无任务详情';
          if (task && (!state.taskLog.done || state.taskLog.taskId !== targetId)) {
            loadTaskLog(targetId);
          }
        }
      } catch (err) {
        if (reqId !== loadTaskDetail._reqSeq) return;
        if (state.nav.view === 'task' && state.nav.id === targetId) {
          if (!hadCurrent) state.taskDetail = null;
          state.taskDetailError = (err && err.message) ? err.message : '任务详情加载失败';
        }
      } finally {
        if (reqId === loadTaskDetail._reqSeq) {
          state.taskDetailLoading = false;
        }
      }
    }
    loadTaskDetail._reqSeq = 0;

    let _taskLogFlushRaf = 0;
    let _taskLogPendingText = '';
    let _taskLogPendingTaskId = null;
    function _cancelTaskLogFlush() {
      if (_taskLogFlushRaf) {
        cancelAnimationFrame(_taskLogFlushRaf);
        _taskLogFlushRaf = 0;
      }
      _taskLogPendingText = '';
      _taskLogPendingTaskId = null;
    }
    function _flushTaskLogPending() {
      if (!_taskLogPendingText || !_taskLogPendingTaskId) return;
      if (state.taskLog.taskId === _taskLogPendingTaskId) {
        state.taskLog.text += _taskLogPendingText;
      }
      _taskLogPendingText = '';
      _taskLogPendingTaskId = null;
    }
    function _queueTaskLogText(taskId, chunk) {
      if (!chunk) return;
      if (state.taskLog.taskId !== taskId) return;
      if (_taskLogPendingTaskId !== taskId) {
        _taskLogPendingText = '';
        _taskLogPendingTaskId = taskId;
      }
      _taskLogPendingText += chunk;
      if (_taskLogFlushRaf) return;
      _taskLogFlushRaf = requestAnimationFrame(() => {
        _taskLogFlushRaf = 0;
        _flushTaskLogPending();
      });
    }

    /* Incremental log loader. If `reset` is true (task changed / first open)
     * we drop the previous buffer and fetch from offset 0; otherwise we ask
     * the backend only for bytes past `nextOffset` and append. Called by:
     *   - selectTask → reset fetch.
     *   - SSE event with task_id == current task → delta fetch.
     *   - loadTaskDetail fallback while the task is still running.
     * Keeps looping until `done` is true so a single change-event can drain
     * multi-chunk backlogs without waiting for the next trigger. */
    async function loadTaskLog(taskId, { reset = false } = {}) {
      if (!taskId) return;
      if (reset || state.taskLog.taskId !== taskId) {
        _cancelTaskLogFlush();
        state.taskLog = { taskId, text: '', nextOffset: 0, size: 0, done: false, loading: false };
      }
      if (state.taskLog.loading) return;
      state.taskLog.loading = true;
      try {
        /* Drain backlogs quickly but stop once the server reports no forward
         * progress; otherwise we'd spin on the same offset and lock the UI. */
        let guard = 64;
        while (guard-- > 0) {
          if (state.taskLog.taskId !== taskId) return;  /* user switched tasks */
          const off = state.taskLog.nextOffset || 0;
          const data = await CP.api.get(`/api/tasks/${taskId}/log?offset=${off}`);
          if (state.taskLog.taskId !== taskId) return;
          const chunk = (data && typeof data.text === 'string') ? data.text : '';
          const nextOffset = Number.isFinite(data && data.next_offset) ? data.next_offset : off;
          if (chunk) _queueTaskLogText(taskId, chunk);
          state.taskLog.nextOffset = nextOffset;
          state.taskLog.size = Number.isFinite(data && data.size) ? data.size : state.taskLog.size;
          state.taskLog.done = !!(data && data.done);
          if (state.taskLog.done) break;
          if (!chunk && nextOffset <= off) break;
        }
      } catch (_err) {
        /* Silent — transient fetch failure; next trigger will retry. */
      } finally {
        if (_taskLogFlushRaf) {
          cancelAnimationFrame(_taskLogFlushRaf);
          _taskLogFlushRaf = 0;
        }
        _flushTaskLogPending();
        state.taskLog.loading = false;
      }
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
        await nextTick();
        if (chatScrollEl) chatScrollEl.scrollTop = chatScrollEl.scrollHeight;
      } catch (err) {
        pushToast(err.message, 'error');
      }
    }

    /* ── Actions ─────────────────────────────────────── */
    async function taskAction(taskId, action) {
      /* Per-task in-flight tracking — previously we set the global
       * `state.sending` which disabled every button on the page for a
       * remote click elsewhere. Now only this task's row shows the
       * pending visual, other interactions stay live. */
      state.pendingTasks[taskId] = action;
      try {
        const out = await CP.api.post(`/api/tasks/${taskId}/${action}`, {});
        pushToast(out.message || '操作完成', 'success');
        /* Optimistically update the task in state immediately so the UI
         * reflects the new status without waiting for the full dashboard
         * round-trip. The subsequent loadDashboard reconciles. */
        if (out && out.task) {
          _mergeTaskIntoState(out.task);
          if (state.nav.view === 'task' && state.nav.id === taskId) {
            state.taskDetail = { ...(state.taskDetail || {}), ...out.task };
          }
        }
        await loadDashboard();
        if (state.nav.view === 'task' && state.nav.id === taskId) await loadTaskDetail();
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        delete state.pendingTasks[taskId];
      }
    }

    /* Merge a single task's fresh payload into every cached location so the
     * UI reflects the update before the next full refresh lands. */
    function _mergeTaskIntoState(t) {
      if (!t || !t.id) return;
      const pname = t.project;
      const patch = (arr) => {
        if (!Array.isArray(arr)) return arr;
        const i = arr.findIndex(x => x.id === t.id);
        if (i >= 0) { arr.splice(i, 1, { ...arr[i], ...t }); return arr; }
        return arr;
      };
      patch(state.tasks);
      if (pname) patch(state.tasksByProject[pname]);
    }

    async function submitGoal() {
      if (!state.nav.project) { pushToast('先选择一个项目', 'error'); return; }
      const text = state.goalText.trim();
      if (!text) { pushToast('输入不能为空', 'error'); return; }
      state.sending = true; state.answer = null;
      try {
        const out = await CP.api.post('/api/goal', {
          project: state.nav.project, text, category: state.goalCategory,
        });
        if (out.intent === 'question' || out.intent === 'command') {
          state.answer = out.message || '完成';
        } else {
          pushToast(out.message || '提交成功', 'success');
          await loadDashboard();
        }
        state.goalText = '';
      } catch (err) {
        pushToast(err.message, 'error');
      } finally { state.sending = false; }
    }

    async function submitComposer() {
      if (!state.nav.project) { pushToast('先选择一个项目', 'error'); return; }
      const title = state.composer.title.trim();
      if (!title) { pushToast('标题不能为空', 'error'); return; }
      state.sending = true;
      const payload = {
        project: state.nav.project, title,
        content: state.composer.content,
        priority: state.composer.priority,
        agent: state.composer.agent,
        planner: state.composer.planner,
        execute: state.composer.execute,
      };
      try {
        let out;
        if (state.composerMode === 'task') {
          out = await CP.api.post('/api/tasks', payload);
          if (out.task) selectTask(state.nav.project, out.task.id);
          state.composer.content = '';
        } else {
          out = await CP.api.post('/api/requirements', payload);
        }
        state.composer.title = '';
        pushToast(out.message || '提交成功', 'success');
        await loadDashboard();
      } catch (err) {
        pushToast(err.message, 'error');
      } finally { state.sending = false; }
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
      const text = state.chatText.trim();
      if (!text) return;
      state.sending = true;
      try {
        await CP.api.post(`/api/sessions/${state.nav.id}/messages`, {
          text, category: state.chatCategory,
        });
        state.chatText = '';
        await loadSessionChat();
        await loadSessions();
      } catch (err) {
        pushToast(err.message, 'error');
      } finally { state.sending = false; }
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
      state.sending = true;
      try {
        await CP.api.del(`/api/sessions/${sid}`);
        setNav({ view: 'sessions', id: null });
        state.sessionDetail = null;
        state.sessionMessages = [];
        await loadSessions();
        pushToast('会话已删除', 'success');
      } catch (err) {
        pushToast(err.message, 'error');
      } finally { state.sending = false; }
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
      if (state.sending) return;
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
    async function submitClarifyAnswer(sessionId, answerText) {
      if (!answerText || !answerText.trim()) return;
      state.sending = true;
      try {
        await CP.api.post(`/api/sessions/${sessionId}/messages`, {
          text: answerText.trim(),
          category: 'auto',
        });
        delete state.clarifyDrafts[sessionId];
        await loadSessionChat();
        await loadSessions();
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.sending = false;
      }
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
      /* actions */
      taskAction, submitGoal, submitComposer,
      toggleProjectForm, submitProject, deleteProject,
      projectService,
      newSession, sendChat, deleteSession,
      submitClarifyAnswer,
      /* Per-task pending helpers for per-row spinners. */
      isTaskPending: (taskId) => !!state.pendingTasks[taskId],
      taskPendingAction: (taskId) => state.pendingTasks[taskId] || '',
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
  },
  template: `
    <div class="shell">
      <cp-sidebar></cp-sidebar>
      <main class="main">
        <cp-main-header></cp-main-header>
        <div class="main-scroll">
          <cp-toast-stack></cp-toast-stack>
          <div v-if="cp.state.daemonHealth && cp.state.daemonHealth.reason && !cp.state.daemonHealth.alive"
               class="daemon-banner"
               :class="cp.state.daemonHealth.running ? 'warn' : 'err'">
            <span class="dot"></span>
            <div class="body">
              <b>daemon {{ cp.state.daemonHealth.running ? '假死' : '已停止' }}</b>：
              {{ cp.state.daemonHealth.reason }}
              <span v-if="cp.state.daemonHealth.last_heartbeat" class="muted tiny">
                · 最后心跳 {{ cp.state.daemonHealth.last_heartbeat }}
              </span>
            </div>
            <div class="hint tiny">
              启动：<code>codepilot daemon -p &lt;project&gt;</code> 或 <code>codepilot webui start</code>
            </div>
          </div>
          <cp-content-pane></cp-content-pane>
        </div>
      </main>
      <cp-confirm-dialog></cp-confirm-dialog>
    </div>
  `,
};

const app = createApp(RootApp);

/* Surface runtime errors instead of silent blank page */
app.config.errorHandler = (err, _instance, info) => {
  // eslint-disable-next-line no-console
  console.error('[CodePilot]', info, err);
  const root = document.getElementById('app');
  if (root && !root.innerHTML.includes('__cp_crash')) {
    root.innerHTML = '<div id="__cp_crash" style="padding:24px;font-family:ui-monospace,monospace;color:#b42318;background:#fef3f2;border:1px solid #fecdca;border-radius:8px;margin:24px;white-space:pre-wrap"><strong>CodePilot UI 崩溃了（按 F12 查看控制台）</strong>\n\n' +
      String(info) + '\n\n' + (err && err.stack ? err.stack : String(err)) + '</div>';
  }
};

CP.install(app);

/* Register every component under CP.Components with kebab-case name `cp-<name>` */
Object.entries(CP.Components).forEach(([name, comp]) => {
  const kebab = 'cp-' + name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
  app.component(kebab, comp);
});

app.mount('#app');
