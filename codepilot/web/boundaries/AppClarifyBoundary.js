/* Clarification boundary extracted from AppStateBoundary.
 * Owns clarify payload parsing, legacy question fallback, and local draft state. */
/* global CP */

window.CP = window.CP || {};

CP.createAppClarifyBoundary = (options = {}) => {
  const state = options.state;
  const pushToast = options.pushToast || (() => {});

  function messageMetadata(message) {
    if (!message || !message.metadata) return {};
    if (typeof message.metadata === 'object') return message.metadata;
    if (typeof message.metadata === 'string') {
      try { return JSON.parse(message.metadata); } catch (_e) { return {}; }
    }
    return {};
  }

  function legacyClarifyQuestionsFromMessage(message) {
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

  function buildClarifyStateFromPayload(payload, options = {}) {
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

  function renderClarifyMessage(message, questions) {
    const base = CP._clarifyText(message) || '为了更好地规划，请先回答几个问题。';
    const details = CP.clarifyQuestionsText(questions);
    return details ? `${base}\n\n${details}` : base;
  }

  function syncSessionClarifyDraft(sessionId, messages) {
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
    const metadata = messageMetadata(pendingMessage);
    const structuredQuestions = CP.normalizeClarifyQuestions(metadata.questions || []);
    const nextDraft = buildClarifyStateFromPayload(
      {
        questions: structuredQuestions.length
          ? structuredQuestions
          : legacyClarifyQuestionsFromMessage(pendingMessage),
      },
      { existing: state.clarifyDrafts[sessionId] || null },
    );
    if (nextDraft) state.clarifyDrafts[sessionId] = nextDraft;
    else delete state.clarifyDrafts[sessionId];
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

  return {
    buildClarifyStateFromPayload,
    renderClarifyMessage,
    syncSessionClarifyDraft,
    cancelGoalClarify,
    cancelComposerClarify,
  };
};
