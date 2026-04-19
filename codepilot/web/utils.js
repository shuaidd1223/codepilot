/* CodePilot shared constants & helpers. Exposes window.CP. */
/* global Vue */

window.CP = window.CP || { Components: {} };

CP.STATUS_LABEL = {
  backlog: '待办', in_progress: '进行中', done: '已完成', failed: '失败',
  cancelled: '已取消', queued: '排队', running: '执行中', succeeded: '成功',
  attention: '需关注', error: '错误', info: '信息', warning: '警告',
  question: '问题', requirement: '需求', task: '任务', command: '命令',
};

CP.PHASE_LABEL = {
  queued: '排队中', planning: '规划中', running: '执行中',
  done: '完成', failed: '失败', attention: '需关注',
};

CP.TONE_MAP = {
  in_progress: 'info', running: 'info', planning: 'info', queued: 'neutral',
  backlog: 'neutral', done: 'success', succeeded: 'success',
  failed: 'danger', error: 'danger', cancelled: 'danger',
  warning: 'warning', attention: 'warning',
  info: 'info', question: 'primary', requirement: 'primary', task: 'primary',
  command: 'neutral',
};

CP.fmtTime = (v) => v ? String(v).replace('T', ' ').slice(0, 19) : '-';
CP.statusLabel = (s) => CP.STATUS_LABEL[s] || s || '-';
CP.phaseLabel = (p) => CP.PHASE_LABEL[p] || p;
CP.toneClass = (s) => CP.TONE_MAP[s] || 'neutral';
CP.isJobActive = (j) => j.status === 'running' || j.status === 'queued';
CP.formatLogs = (logs) => logs
  .map(i => `[${i.phase || '-'}] agent=${i.agent || '-'} exit=${i.exit_code == null ? '-' : i.exit_code}\n${i.output_excerpt || ''}`)
  .join('\n\n');

CP.api = {
  async get(url) {
    const r = await fetch(url);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText || '请求失败');
    return d;
  },
  async post(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText || '请求失败');
    return d;
  },
  async del(url) {
    const r = await fetch(url, { method: 'DELETE' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText || '请求失败');
    return d;
  },
};

/* Server-sent events client. Wraps EventSource with a small reconnect policy
 * so dashboard panels can subscribe to live progress without rolling their
 * own. Returns a handle with .close() for teardown. */
CP.sse = {
  open(url, onEvent, onError) {
    let es = null;
    let closed = false;
    const connect = () => {
      if (closed) return;
      try {
        es = new EventSource(url);
      } catch (err) {
        if (onError) onError(err);
        return;
      }
      es.onmessage = (ev) => {
        if (!ev || !ev.data) return;
        try {
          onEvent(JSON.parse(ev.data));
        } catch (err) {
          /* ignore malformed frames */
        }
      };
      es.onerror = () => {
        if (closed) return;
        try { es.close(); } catch (e) { /* ignore */ }
        /* Browser EventSource auto-reconnects on most transient failures.
         * We only re-open if the connection fully closed. */
        setTimeout(connect, 2000);
      };
    };
    connect();
    return {
      close() {
        closed = true;
        if (es) try { es.close(); } catch (e) { /* ignore */ }
      },
    };
  },
};

/* Register helper so components can use `this.$cp.fmtTime(...)` etc. */
CP.install = (app) => {
  app.config.globalProperties.$cp = {
    fmtTime: CP.fmtTime,
    statusLabel: CP.statusLabel,
    phaseLabel: CP.phaseLabel,
    toneClass: CP.toneClass,
    isJobActive: CP.isJobActive,
    formatLogs: CP.formatLogs,
  };
};
