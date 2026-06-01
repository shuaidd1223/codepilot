/* UI feedback/actions boundary extracted from AppStateBoundary.
 * Owns scoped pending flags, toast lifecycle, confirm dialog, and chat-scroll registration. */
/* global CP */

window.CP = window.CP || {};

CP.createAppFeedbackBoundary = (options = {}) => {
  const state = options.state;

  let chatScrollEl = null;
  let confirmResolver = null;
  const toastTimers = new Map();

  const ACTION_KEYS = Object.freeze({
    SESSION_SEND: 'session.send',
    SESSION_STOP: 'session.stop',
    SESSION_DELETE: 'session.delete',
    JOB_ACTION: 'job.action',
  });

  const refreshBlockingActions = new Set([
    ACTION_KEYS.SESSION_SEND,
    ACTION_KEYS.SESSION_STOP,
    ACTION_KEYS.SESSION_DELETE,
  ]);

  function registerChatScroll(el) {
    chatScrollEl = el;
  }

  function getChatScrollEl() {
    return chatScrollEl;
  }

  function syncLegacySending() {
    state.sending = Object.values(state.actionPending || {}).some(Boolean);
  }

  function isActionPending(actionKey) {
    if (!actionKey) return false;
    return !!state.actionPending[actionKey];
  }

  function setActionPending(actionKey, pending) {
    if (!actionKey) return;
    state.actionPending[actionKey] = !!pending;
    syncLegacySending();
  }

  function isRefreshBlocked() {
    for (const key of refreshBlockingActions) {
      if (isActionPending(key)) return true;
    }
    return false;
  }

  async function runScopedAction(actionKey, runner) {
    if (isActionPending(actionKey)) return false;
    setActionPending(actionKey, true);
    try {
      await runner();
      return true;
    } finally {
      setActionPending(actionKey, false);
    }
  }

  const MAX_VISIBLE_TOASTS = 3;

  function pushToast(message, type = 'info') {
    const existing = state.toasts.find((toast) => toast.message === message && toast.type === type);
    if (existing) {
      existing.count = (existing.count || 1) + 1;
      if (type !== 'error') {
        clearTimeout(toastTimers.get(existing.id));
        toastTimers.set(existing.id, setTimeout(() => dismissToast(existing.id), 4000));
      }
      return;
    }
    const id = ++state.toastSeq;
    state.toasts.push({ id, message, type, count: 1 });
    // Enforce max visible toast limit: remove oldest non-error first
    while (state.toasts.length > MAX_VISIBLE_TOASTS) {
      const idx = state.toasts.findIndex(t => t.type !== 'error');
      if (idx >= 0) {
        const removed = state.toasts[idx];
        clearTimeout(toastTimers.get(removed.id));
        toastTimers.delete(removed.id);
        state.toasts.splice(idx, 1);
      } else break;
    }
    if (type !== 'error') {
      toastTimers.set(id, setTimeout(() => dismissToast(id), 4000));
    }
  }

  function dismissToast(id) {
    const timer = toastTimers.get(id);
    if (timer) {
      clearTimeout(timer);
      toastTimers.delete(id);
    }
    state.toasts = state.toasts.filter((toast) => toast.id !== id);
  }

  function resolveConfirm(result) {
    const resolver = confirmResolver;
    confirmResolver = null;
    state.confirmDialog.open = false;
    if (resolver) resolver(!!result);
    processConfirmQueue();
  }

  function confirmDialog(options = {}) {
    const opts = options || {};
    return new Promise((resolve) => {
      if (confirmResolver) resolveConfirm(false);
      state.confirmDialog.title = opts.title || '请确认';
      state.confirmDialog.message = opts.message || '';
      state.confirmDialog.confirmText = opts.confirmText || '确认';
      state.confirmDialog.cancelText = opts.cancelText || '取消';
      state.confirmDialog.tone = opts.tone || 'danger';
      state.confirmDialog.open = true;
      confirmResolver = resolve;
    });
  }

  return {
    ACTION_KEYS,
    registerChatScroll,
    getChatScrollEl,
    isActionPending,
    isRefreshBlocked,
    runScopedAction,
    pushToast,
    dismissToast,
    confirmDialog,
    resolveConfirm,
  };
};
