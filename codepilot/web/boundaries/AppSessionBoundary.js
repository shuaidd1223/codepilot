/* Session/chat boundary extracted from AppStateBoundary.
 * Owns session detail fetch, chat send/delete, and clarify-reply actions. */
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
  const selectSession = options.selectSession || (() => {});
  const setNav = options.setNav || (() => {});
  const buildClarifyStateFromPayload = options.buildClarifyStateFromPayload || (() => null);
  const syncSessionClarifyDraft = options.syncSessionClarifyDraft || (() => {});

  async function loadSessionChat() {
    if (state.nav.view !== 'session' || !state.nav.id) return;
    const targetId = state.nav.id;
    try {
      const data = await CP.api.get(`/api/sessions/${targetId}`);
      if (state.nav.view !== 'session' || state.nav.id !== targetId) return;
      state.sessionDetail = data.session;
      state.sessionMessages = data.messages || [];
      syncSessionClarifyDraft(targetId, state.sessionMessages);
      await nextTick();
      const chatScrollEl = getChatScrollEl();
      if (chatScrollEl) chatScrollEl.scrollTop = chatScrollEl.scrollHeight;
    } catch (err) {
      pushToast(err.message, 'error');
    }
  }

  async function newSession() {
    if (!state.nav.project) {
      pushToast('先选择一个项目', 'error');
      return;
    }
    state.newSessionLoading = true;
    try {
      const out = await CP.api.post('/api/sessions', { project: state.nav.project, title: '' });
      await loadSessions();
      selectSession(state.nav.project, out.session.id);
      pushToast('会话已创建', 'success');
    } catch (err) {
      pushToast(err.message, 'error');
    } finally {
      state.newSessionLoading = false;
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
    await runScopedAction(ACTION_KEYS.SESSION_SEND, async () => {
      try {
        await CP.api.post(`/api/sessions/${state.nav.id}/messages`, {
          text,
          category: state.chatCategory,
        });
        state.chatText = '';
        await loadSessionChat();
        await loadSessions();
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
    await runScopedAction(ACTION_KEYS.SESSION_DELETE, async () => {
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

  async function submitClarifyAnswer(sessionId, answerText = '') {
    const draft = state.clarifyDrafts[sessionId] || null;
    const clarifyAnswers = draft
      ? CP.exportClarifyAnswers(draft.questions, draft.answers)
      : [];
    const text = String(answerText || '').trim();
    if (!text && !clarifyAnswers.length) return;
    await runScopedAction(ACTION_KEYS.SESSION_CLARIFY_REPLY, async () => {
      try {
        const out = await CP.api.post(`/api/sessions/${sessionId}/messages`, {
          text,
          category: 'auto',
          clarify_answers: clarifyAnswers,
        });
        if (out.intent === 'clarify') {
          state.clarifyDrafts[sessionId] = buildClarifyStateFromPayload(
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

  return {
    loadSessionChat,
    newSession,
    sendChat,
    cancelSessionClarify,
    deleteSession,
    submitClarifyAnswer,
  };
};
