/* Session/chat boundary extracted from AppStateBoundary.
 * Owns session detail fetch, chat send/delete, embedded chat, and clarify-reply actions. */
/* global CP */

window.CP = window.CP || {};

CP.createAppSessionBoundary = (options = {}) => {
  const state = options.state;
  const nextTick = options.nextTick || (async () => {});
  const pushToast = options.pushToast || (() => {});
  const runScopedAction = options.runScopedAction || (async (_key, runner) => runner());
  const ACTION_KEYS = options.ACTION_KEYS || {};
  const getChatScrollEl = options.getChatScrollEl || (() => null);
  const loadSessions = options.loadSessions || (async () => {});
  const confirmDialog = options.confirmDialog || (async () => false);
  const setNav = options.setNav || (() => {});
  const openProjectCategory = options.openProjectCategory || (() => {});
  const syncSessionClarifyDraft = options.syncSessionClarifyDraft || (() => {});

  function currentSessionId() {
    if (state.nav.view === 'session' && state.nav.id) return Number(state.nav.id);
    return Number(state.activeProjectSessionId || 0);
  }

  function isCurrentSession(sessionId) {
    const id = Number(sessionId || 0);
    if (!id) return false;
    if (state.nav.view === 'session') return Number(state.nav.id) === id;
    return Number(state.activeProjectSessionId || 0) === id;
  }

  function projectSessions(project = state.nav.project) {
    return (state.sessions || []).filter((session) => session && session.project === project);
  }

  async function loadSessionChat(sessionId = null) {
    const targetId = Number(sessionId || currentSessionId() || 0);
    if (!targetId) return;
    try {
      const data = await CP.api.get(`/api/sessions/${targetId}`);
      const loadedSession = data.session || null;
      if (!loadedSession) return;
      if (state.nav.view === 'session') {
        if (Number(state.nav.id) !== targetId) return;
      } else if (state.nav.project && loadedSession.project !== state.nav.project) {
        return;
      } else if (!isCurrentSession(targetId)) {
        return;
      }
      if (loadedSession.project === state.nav.project) {
        state.activeProjectSessionId = targetId;
      }
      state.sessionDetail = loadedSession;
      state.sessionMessages = data.messages || [];
      syncSessionClarifyDraft(targetId, state.sessionMessages);
      await nextTick();
      const chatScrollEl = getChatScrollEl();
      if (chatScrollEl) chatScrollEl.scrollTop = chatScrollEl.scrollHeight;
    } catch (err) {
      pushToast(err.message, 'error');
    }
  }

  function ensureProjectSessionSelected(project = state.nav.project, { load = false } = {}) {
    if (!project) return 0;
    const current = Number(state.activeProjectSessionId || 0);
    if (current && projectSessions(project).some((session) => Number(session.id) === current)) {
      if (load && (!state.sessionDetail || Number(state.sessionDetail.id) !== current)) {
        loadSessionChat(current);
      }
      return current;
    }
    const latest = projectSessions(project)[0] || null;
    const nextId = latest ? Number(latest.id) : 0;
    state.activeProjectSessionId = nextId || null;
    if (!nextId) {
      if (state.nav.view !== 'session') {
        state.sessionDetail = null;
        state.sessionMessages = [];
      }
      return 0;
    }
    if (load) loadSessionChat(nextId);
    return nextId;
  }

  async function selectEmbeddedSession(project, sessionId) {
    const targetProject = project || state.nav.project;
    const targetId = Number(sessionId || 0);
    if (!targetProject || !targetId) return;
    state.expanded[targetProject] = true;
    openProjectCategory(targetProject, 'sessions');
    setNav({ project: targetProject, view: 'overview', id: null });
    state.activeProjectSessionId = targetId;
    state.sessionDetail = null;
    state.sessionMessages = [];
    await loadSessionChat(targetId);
  }

  async function openSessionPage(project, sessionId) {
    const targetProject = project || state.nav.project;
    const targetId = Number(sessionId || state.activeProjectSessionId || 0);
    if (!targetProject || !targetId) return;
    state.expanded[targetProject] = true;
    openProjectCategory(targetProject, 'sessions');
    setNav({ project: targetProject, view: 'session', id: targetId });
    state.activeProjectSessionId = targetId;
    await loadSessionChat(targetId);
  }

  async function newSession(projectOrOptions = null, maybeOptions = {}) {
    let project = state.nav.project;
    let options = maybeOptions || {};
    if (typeof projectOrOptions === 'string') {
      project = projectOrOptions || project;
    } else if (projectOrOptions && typeof projectOrOptions === 'object') {
      options = projectOrOptions;
    }
    if (!project) {
      pushToast('先选择一个项目', 'error');
      return null;
    }
    state.newSessionLoading = true;
    try {
      const out = await CP.api.post('/api/sessions', { project, title: '' });
      await loadSessions();
      const sessionId = Number(out.session && out.session.id);
      if (!sessionId) return out.session || null;
      if (options.openAdvanced || state.nav.view === 'session') {
        await openSessionPage(project, sessionId);
      } else {
        await selectEmbeddedSession(project, sessionId);
      }
      if (!options.silent) pushToast('会话已创建', 'success');
      return out.session || null;
    } catch (err) {
      pushToast(err.message, 'error');
      return null;
    } finally {
      state.newSessionLoading = false;
    }
  }

  async function ensureProjectSessionForSend() {
    if (state.nav.view === 'session' && state.nav.id) return Number(state.nav.id);
    const existing = ensureProjectSessionSelected(state.nav.project, { load: false });
    if (existing) return existing;
    const created = await newSession(state.nav.project, { silent: true });
    return created && created.id ? Number(created.id) : 0;
  }

  async function sendChat() {
    const text = state.chatText.trim();
    const files = state._pendingFiles || [];
    delete state._pendingFiles;
    if (!text && !files.length) return;
    const sessionId = await ensureProjectSessionForSend();
    if (!sessionId) return;
    state.chatText = '';
    await runScopedAction(ACTION_KEYS.SESSION_SEND, async () => {
      try {
        const body = {
          text,
          run_async: true,
          runtime: state.opencodeRuntime || {},
        };
        if (files.length) body.files = files;
        const out = await CP.api.post(`/api/sessions/${sessionId}/messages`, body);
        if (out.user_message && out.assistant_message && isCurrentSession(sessionId)) {
          const existingIds = new Set((state.sessionMessages || []).map((msg) => Number(msg.id)));
          if (!existingIds.has(Number(out.user_message.id))) state.sessionMessages.push(out.user_message);
          if (!existingIds.has(Number(out.assistant_message.id))) state.sessionMessages.push(out.assistant_message);
          if (out.assistant_message_id) {
            state.sessionRuns[String(out.assistant_message_id)] = {
              session_id: sessionId,
              assistant_message_id: out.assistant_message_id,
              status: 'running',
              content_snapshot: '',
              tool_calls: [],
              events: [],
            };
          }
          await nextTick();
          const chatScrollEl = getChatScrollEl();
          if (chatScrollEl) chatScrollEl.scrollTop = chatScrollEl.scrollHeight;
        } else {
          await loadSessionChat(sessionId);
        }
        await loadSessions();
      } catch (err) {
        state.chatText = text;
        pushToast(err.message, 'error');
      }
    });
  }

  async function sendEmbeddedChat() {
    if (!state.nav.project) {
      pushToast('先选择一个项目', 'error');
      return;
    }
    if (state.nav.view !== 'overview') {
      setNav({ view: 'overview', id: null });
    }
    await sendChat();
  }

  async function stopSessionRun(messageId = null) {
    const sessionId = currentSessionId();
    if (!sessionId) return;
    const resolvedMessageId = messageId || Object.values(state.sessionRuns || {})
      .filter((run) => run && Number(run.session_id) === sessionId && run.status === 'running')
      .map((run) => Number(run.assistant_message_id || 0))
      .filter(Boolean)
      .pop();
    if (!resolvedMessageId) return;
    const key = `${ACTION_KEYS.SESSION_STOP}:${resolvedMessageId}`;
    await runScopedAction(key, async () => {
      try {
        await CP.api.post(`/api/sessions/${sessionId}/runs/${resolvedMessageId}/stop`, {});
      } catch (err) {
        pushToast(err.message, 'error');
      }
    });
  }

  async function cancelSessionClarify(sessionId) {
    if (!sessionId) return;
    await runScopedAction(ACTION_KEYS.SESSION_CLARIFY_CANCEL, async () => {
      try {
        await CP.api.post(`/api/sessions/${sessionId}/messages`, {
          text: '/clear',
          category: 'auto',
        });
        delete state.clarifyDrafts[sessionId];
        if (isCurrentSession(sessionId)) {
          await loadSessionChat(sessionId);
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
    await runScopedAction(ACTION_KEYS.SESSION_DELETE, async () => {
      try {
        await CP.api.del(`/api/sessions/${sid}`);
        setNav({ view: 'sessions', id: null });
        if (Number(state.activeProjectSessionId) === Number(sid)) state.activeProjectSessionId = null;
        state.sessionDetail = null;
        state.sessionMessages = [];
        await loadSessions();
        pushToast('会话已删除', 'success');
      } catch (err) {
        pushToast(err.message, 'error');
      }
    });
  }

  async function submitClarifyAnswer(sessionId, answerText = '') {
    const draft = state.clarifyDrafts[sessionId] || null;
    const clarifyAnswers = draft
      ? CP.exportClarifyAnswers(draft.questions, draft.answers)
      : [];
    const text = String(answerText || '').trim();
    if (!text && !clarifyAnswers.length) return;
    await runScopedAction(ACTION_KEYS.SESSION_CLARIFY_REPLY, async () => {
      try {
        await CP.api.post(`/api/sessions/${sessionId}/messages`, {
          text,
          category: 'auto',
          clarify_answers: clarifyAnswers,
        });
        delete state.clarifyDrafts[sessionId];
        await loadSessionChat(sessionId);
        await loadSessions();
      } catch (err) {
        pushToast(err.message, 'error');
      }
    });
  }

  return {
    loadSessionChat,
    ensureProjectSessionSelected,
    selectEmbeddedSession,
    openSessionPage,
    newSession,
    sendChat,
    sendEmbeddedChat,
    stopSessionRun,
    cancelSessionClarify,
    deleteSession,
    submitClarifyAnswer,
  };
};
