/* Task-detail boundary extracted from app.js.
 * Owns task detail fetch, incremental task log stream, and task actions. */
/* global CP */

window.CP = window.CP || {};

CP.createTaskDetailBoundary = (options = {}) => {
  const state = options.state;
  const pushToast = options.pushToast || (() => {});
  const loadDashboard = options.loadDashboard || (async () => {});

  let taskDetailReqSeq = 0;
  let taskLogFlushRaf = 0;
  let taskLogPendingText = '';
  let taskLogPendingTaskId = null;
  let taskLogReloadTaskId = null;

  function cancelTaskLogFlush() {
    if (taskLogFlushRaf) {
      cancelAnimationFrame(taskLogFlushRaf);
      taskLogFlushRaf = 0;
    }
    taskLogPendingText = '';
    taskLogPendingTaskId = null;
  }

  function flushTaskLogPending() {
    if (!taskLogPendingText || !taskLogPendingTaskId) return;
    if (state.taskLog.taskId === taskLogPendingTaskId) {
      state.taskLog.text += taskLogPendingText;
    }
    taskLogPendingText = '';
    taskLogPendingTaskId = null;
  }

  function queueTaskLogText(taskId, chunk) {
    if (!chunk) return;
    if (state.taskLog.taskId !== taskId) return;
    if (taskLogPendingTaskId !== taskId) {
      taskLogPendingText = '';
      taskLogPendingTaskId = taskId;
    }
    taskLogPendingText += chunk;
    if (taskLogFlushRaf) return;
    taskLogFlushRaf = requestAnimationFrame(() => {
      taskLogFlushRaf = 0;
      flushTaskLogPending();
    });
  }

  function asFiniteNumber(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  async function loadTaskDetail({ silent = false } = {}) {
    if (state.nav.view !== 'task' || !state.nav.id) return;
    const targetId = state.nav.id;
    const hadCurrent = !!(state.taskDetail && state.taskDetail.id === targetId);
    if (!silent && !hadCurrent) state.taskDetailLoading = true;
    state.taskDetailError = '';
    const reqId = ++taskDetailReqSeq;
    try {
      const data = await CP.api.get(`/api/tasks/${targetId}`);
      if (reqId !== taskDetailReqSeq) return;
      if (state.nav.view === 'task' && state.nav.id === targetId) {
        const task = (data && typeof data === 'object' && data.id) ? data : null;
        state.taskDetail = task;
        state.taskDetailError = task ? '' : '暂无任务详情';
        if (task && (!state.taskLog.done || state.taskLog.taskId !== targetId)) {
          loadTaskLog(targetId);
        }
      }
    } catch (err) {
      if (reqId !== taskDetailReqSeq) return;
      if (state.nav.view === 'task' && state.nav.id === targetId) {
        if (!hadCurrent) state.taskDetail = null;
        state.taskDetailError = (err && err.message) ? err.message : '任务详情加载失败';
      }
    } finally {
      if (reqId === taskDetailReqSeq) {
        state.taskDetailLoading = false;
      }
    }
  }

  async function loadTaskLog(taskId, { reset = false } = {}) {
    if (!taskId) return;
    if (reset || state.taskLog.taskId !== taskId) {
      cancelTaskLogFlush();
      taskLogReloadTaskId = null;
      state.taskLog = { taskId, text: '', nextOffset: 0, size: 0, done: false, loading: false };
    }
    if (state.taskLog.loading) {
      taskLogReloadTaskId = taskId;
      return;
    }
    state.taskLog.loading = true;
    try {
      let guard = 64;
      while (guard-- > 0) {
        if (state.taskLog.taskId !== taskId) return;
        const off = state.taskLog.nextOffset || 0;
        const data = await CP.api.get(`/api/tasks/${taskId}/log?offset=${off}`);
        if (state.taskLog.taskId !== taskId) return;
        const currentOffset = Number.isFinite(state.taskLog.nextOffset) ? state.taskLog.nextOffset : 0;
        if (currentOffset !== off) {
          /* A newer SSE chunk already advanced the cursor while this request
           * was in-flight. Skip stale payload to avoid duplicated segments. */
          continue;
        }
        const chunk = (data && typeof data.text === 'string') ? data.text : '';
        const nextOffset = Number.isFinite(data && data.next_offset) ? data.next_offset : off;
        if (chunk) queueTaskLogText(taskId, chunk);
        state.taskLog.nextOffset = nextOffset;
        state.taskLog.size = Number.isFinite(data && data.size) ? data.size : state.taskLog.size;
        state.taskLog.done = !!(data && data.done);
        if (state.taskLog.done) break;
        if (!chunk && nextOffset <= off) break;
      }
    } catch (_err) {
      /* transient fetch failure; next trigger retries */
    } finally {
      if (taskLogFlushRaf) {
        cancelAnimationFrame(taskLogFlushRaf);
        taskLogFlushRaf = 0;
      }
      flushTaskLogPending();
      state.taskLog.loading = false;
      if (taskLogReloadTaskId === taskId && state.taskLog.taskId === taskId) {
        taskLogReloadTaskId = null;
        loadTaskLog(taskId).catch(() => {});
      }
    }
  }

  function handleTaskLogStreamEvent(event) {
    const extra = (event && event.extra) || null;
    if (!extra || !extra.task_log_stream) return false;
    const tid = state.nav.view === 'task' ? state.nav.id : null;
    if (!tid || event.task_id !== tid) return true;
    if (state.taskLog.taskId !== tid) {
      loadTaskLog(tid, { reset: true });
      return true;
    }
    const chunk = (typeof extra.task_log_chunk === 'string') ? extra.task_log_chunk : '';
    const start = asFiniteNumber(extra.task_log_start);
    const end = asFiniteNumber(extra.task_log_end);
    const expected = asFiniteNumber(state.taskLog.nextOffset) || 0;
    if (!chunk || start == null || end == null || end < start) {
      loadTaskLog(tid);
      return true;
    }
    if (start !== expected) {
      loadTaskLog(tid);
      return true;
    }
    queueTaskLogText(tid, chunk);
    state.taskLog.nextOffset = end;
    state.taskLog.size = Math.max(state.taskLog.size || 0, end);
    state.taskLog.done = false;
    return true;
  }

  async function taskAction(taskId, action) {
    state.pendingTasks[taskId] = action;
    try {
      const out = await CP.api.post(`/api/tasks/${taskId}/${action}`, {});
      pushToast(out.message || '操作完成', 'success');
      if (out && out.task) {
        CP.StateBoundary.mergeTaskIntoState(state, out.task);
        if (state.nav.view === 'task' && state.nav.id === taskId) {
          state.taskDetail = { ...(state.taskDetail || {}), ...out.task };
        }
      }
      await loadDashboard();
      if (state.nav.view === 'task' && state.nav.id === taskId) {
        await loadTaskDetail();
      }
    } catch (err) {
      pushToast(err.message, 'error');
    } finally {
      delete state.pendingTasks[taskId];
    }
  }

  return {
    loadTaskDetail,
    loadTaskLog,
    handleTaskLogStreamEvent,
    taskAction,
  };
};
