/* CodePilot main app — state, navigation, actions, root component. */
/* global Vue, CP */

const { createApp, reactive, computed, onMounted, onUnmounted, nextTick, ref, provide } = Vue;

const RootApp = {
  setup() {
    /* ── Reactive state ──────────────────────────────── */
    const state = reactive({
      /* data from server */
      projects: [], tasks: [], jobs: [], events: [], sessions: [],

      /* navigation: {project, view, id} */
      nav: { project: null, view: 'overview', id: null },
      expanded: {},  /* tree-node key → boolean */

      /* detail caches keyed by navigation */
      taskDetail: null,
      sessionDetail: null,
      sessionMessages: [],

      /* global UI */
      autoRefresh: true, timer: null,
      loading: false, sending: false, newSessionLoading: false,
      dark: false,
      toasts: [], toastSeq: 0,
      answer: null,

      /* forms */
      goalText: '', goalCategory: 'auto',
      composerMode: 'requirement',
      composer: { title: '', content: '', priority: 'P2', agent: 'auto', planner: 'codex', execute: true },
      chatText: '', chatCategory: 'auto',
    });

    let chatScrollEl = null;
    const registerChatScroll = (el) => { chatScrollEl = el; };

    /* ── Toast helpers ───────────────────────────────── */
    function pushToast(message, type = 'info') {
      const id = ++state.toastSeq;
      state.toasts.push({ id, message, type });
      if (type !== 'error') setTimeout(() => dismissToast(id), 4000);
    }
    function dismissToast(id) { state.toasts = state.toasts.filter(t => t.id !== id); }

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

    /* ── Navigation ──────────────────────────────────── */
    function setNav(partial) {
      Object.assign(state.nav, partial);
    }
    function toggleExpanded(key) {
      state.expanded[key] = !state.expanded[key];
    }
    function isExpanded(key, defaultOpen = false) {
      return state.expanded[key] === undefined ? defaultOpen : state.expanded[key];
    }

    function selectProject(name) {
      /* click on project name — select + show overview, auto-expand */
      state.expanded[name] = true;
      setNav({ project: name, view: 'overview', id: null });
      loadDashboard();
    }
    function toggleProject(name) {
      /* click on chevron — just toggle expand */
      state.expanded[name] = !state.expanded[name];
    }
    function selectCategory(project, view) {
      /* view ∈ 'sessions' | 'tasks' | 'jobs' */
      state.expanded[project] = true;
      state.expanded[`${project}/${view}`] = true;
      setNav({ project, view, id: null });
    }
    function selectSession(project, id) {
      state.expanded[project] = true;
      state.expanded[`${project}/sessions`] = true;
      setNav({ project, view: 'session', id });
      loadSessionChat();
    }
    function selectTask(project, id) {
      state.expanded[project] = true;
      state.expanded[`${project}/tasks`] = true;
      setNav({ project, view: 'task', id });
      loadTaskDetail();
    }
    function selectJob(project, id) {
      state.expanded[project] = true;
      state.expanded[`${project}/jobs`] = true;
      setNav({ project, view: 'job', id });
    }

    function toggleAuto() { state.autoRefresh = !state.autoRefresh; schedule(); }
    function toggleDark() {
      state.dark = !state.dark;
      document.documentElement.dataset.theme = state.dark ? 'dark' : 'light';
      try { localStorage.setItem('cp-dark', state.dark ? '1' : '0'); } catch (e) { /* ignore */ }
    }

    /* ── Data loading ────────────────────────────────── */
    async function loadDashboard() {
      if (state.sending) return;
      state.loading = true;
      try {
        const url = state.nav.project
          ? `/api/projects/${encodeURIComponent(state.nav.project)}`
          : '/api/projects';
        const data = await CP.api.get(url);
        state.projects = data.projects || [];
        state.tasks = data.tasks || [];
        state.jobs = data.jobs || [];
        state.events = data.events || [];
        /* auto-pick first project on first load */
        if (!state.nav.project && data.selected_project) {
          state.nav.project = data.selected_project;
          state.expanded[data.selected_project] = true;
        }
        await loadSessions();
        /* if viewing a task/session that went away, fall back */
        if (state.nav.view === 'task' && state.nav.id) {
          if (!state.tasks.find(t => t.id === state.nav.id)) {
            setNav({ view: 'overview', id: null });
          } else {
            await loadTaskDetail();
          }
        }
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.loading = false;
      }
    }

    async function loadTaskDetail() {
      if (state.nav.view !== 'task' || !state.nav.id) return;
      try {
        state.taskDetail = await CP.api.get(`/api/tasks/${state.nav.id}`);
      } catch (err) {
        state.taskDetail = null;
      }
    }

    async function loadSessions() {
      try {
        const url = state.nav.project
          ? `/api/sessions?project=${encodeURIComponent(state.nav.project)}`
          : '/api/sessions';
        const data = await CP.api.get(url);
        state.sessions = data.sessions || [];
      } catch (err) { /* silent */ }
    }

    async function loadSessionChat() {
      if (state.nav.view !== 'session' || !state.nav.id) return;
      try {
        const data = await CP.api.get(`/api/sessions/${state.nav.id}`);
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
      state.sending = true;
      try {
        const out = await CP.api.post(`/api/tasks/${taskId}/${action}`, {});
        pushToast(out.message || '操作完成', 'success');
        await loadDashboard();
        if (state.nav.view === 'task' && state.nav.id === taskId) await loadTaskDetail();
      } catch (err) {
        pushToast(err.message, 'error');
      } finally {
        state.sending = false;
      }
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
      if (!confirm('确定要删除这个会话吗？')) return;
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

    /* ── Auto refresh ────────────────────────────────── */
    function schedule() {
      clearInterval(state.timer);
      if (!state.autoRefresh) return;
      state.timer = setInterval(() => {
        if (state.sending) return;
        loadDashboard();
        if (state.nav.view === 'session' && state.nav.id) loadSessionChat();
      }, 3000);
    }

    onMounted(() => {
      try { state.dark = localStorage.getItem('cp-dark') === '1'; } catch (e) { /* ignore */ }
      document.documentElement.dataset.theme = state.dark ? 'dark' : 'light';
      loadDashboard();
      schedule();
    });
    onUnmounted(() => clearInterval(state.timer));

    /* ── Provided to all descendants ──────────────────
     * Computed refs are NOT auto-unwrapped when accessed via `cp.xxx`,
     * so we expose them through getters that read `.value` internally. */
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
      selectProject, toggleProject, selectCategory,
      selectSession, selectTask, selectJob,
      toggleAuto, toggleDark,
      /* data */
      loadDashboard, loadTaskDetail, loadSessions, loadSessionChat,
      /* actions */
      taskAction, submitGoal, submitComposer,
      newSession, sendChat, deleteSession,
      pushToast, dismissToast,
      registerChatScroll,
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
          <cp-content-pane></cp-content-pane>
        </div>
      </main>
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
