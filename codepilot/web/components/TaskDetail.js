/* Task detail view. */
/* global Vue, CP */
CP.Components.TaskDetail = Vue.defineComponent({
  name: 'CpTaskDetail',
  inject: ['cp'],
  data() {
    return {
      expandedPhaseKeys: {},
      phaseRawLogs: {},
      phaseRawLoading: {},
      phaseRawErrors: {},
      showFullTaskContent: false,
    };
  },
  computed: {
    s() { return this.cp.state; },
    task() { return this.s.taskDetail; },
    taskId() { return this.task && this.task.id; },
    logText() {
      const streamed = (this.s.taskLog && this.s.taskLog.text) || '';
      if (streamed) return streamed;
      return (this.task && this.task.log_text) || '';
    },
    phaseLogs() {
      const task = this.task;
      return (task && Array.isArray(task.phase_logs)) ? task.phase_logs : [];
    },
    processPhases() {
      return this.phaseLogs.filter(phase => phase && phase.key);
    },
    logFileName() {
      const task = this.task;
      if (!task || !task.current_log_path) return 'log';
      const parts = String(task.current_log_path).split(/[\\/]/);
      return parts[parts.length - 1] || 'log';
    },
    logStats() {
      const raw = this.logText;
      if (!raw) return { lines: 0, tools: 0, diffs: 0, warns: 0 };
      const rows = raw.split(/\r?\n/);
      let lines = 0;
      let tools = 0;
      let diffs = 0;
      let warns = 0;
      for (const row of rows) {
        const line = String(row || '');
        if (line.trim()) lines += 1;
        if (/^\s*[⏺●◉▲▸▶]\s+[A-Za-z_][\w.-]*/.test(line)) tools += 1;
        if (/^diff --git /.test(line)) diffs += 1;
        if (/^\s*[✗✖✘×⚠⚡]\s/.test(line) || /\b(error|failed|exception|traceback)\b/i.test(line)) warns += 1;
      }
      return { lines, tools, diffs, warns };
    },
    logSummaryText() {
      if (!this.task) return '';
      if (!this.logText) {
        if (this.s.taskLog && this.s.taskLog.loading) return '日志加载中...';
        return '当前暂无日志输出';
      }
      const done = !!(this.s.taskLog && this.s.taskLog.done);
      if (!done || this.task.status === 'in_progress') return '流式输出中，自动跟随最新内容';
      return '日志流已结束，可回溯查看完整上下文';
    },
    logTitle() {
      const t = this.task;
      return `${(t && t.agent) || 'agent'} · ${this.logFileName}`;
    },
    runStatus() {
      return this.s.taskRunStatus || {};
    },
    runElapsedSeconds() {
      const task = this.task;
      if (!task || !task.started_at || !(task.status === 'in_progress')) return 0;
      const now = Date.now();
      const start = new Date(task.started_at).getTime();
      return Math.max(0, Math.floor((now - start) / 1000));
    },
    runIdleSeconds() {
      const rs = this.runStatus;
      if (rs && rs.task_id === this.task && this.task.id) {
        return Number.isFinite(rs.silent_seconds) ? rs.silent_seconds : 0;
      }
      return 0;
    },
    isRunIdle() {
      return this.runIdleSeconds >= 3;
    },
    runElapsedLabel() {
      const s = this.runElapsedSeconds;
      if (!s) return '';
      if (s < 60) return `${s}s`;
      if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      return `${h}h ${m}m`;
    },
    runStatusLabel() {
      if (!this.task || this.task.status !== 'in_progress') return '';
      if (this.isRunIdle) return '等待模型响应...';
      return '运行中';
    },
    canSplit() {
      const t = this.task;
      if (!t) return false;
      /* Offer split on failed / attention / backlog tasks where the user
       * likely regrets the scope. Not on in-progress / done. */
      return ['failed', 'cancelled', 'backlog'].includes(t.status);
    },
    latestReview() {
      /* The server attaches parsed reviewer verdict to each reviewer-phase
       * log entry plus a top-level latest_review for convenience. Prefer the
       * server's top-level field; fall back to scanning logs in reverse so
       * older payloads keep working. */
      const t = this.task;
      if (!t) return null;
      if (t.latest_review) return t.latest_review;
      if (!Array.isArray(t.logs)) return null;
      for (let i = t.logs.length - 1; i >= 0; i -= 1) {
        const entry = t.logs[i];
        if (entry && entry.review) {
          return { ...entry.review, phase: entry.phase, agent: entry.agent };
        }
      }
      return null;
    },
    executionArtifacts() {
      return (this.task && this.task.artifacts) || {};
    },
    hasExecutionArtifacts() {
      const a = this.executionArtifacts;
      return !!(a.patch || a.validation || a.review);
    },
    timelineEvents() {
      const t = this.task;
      const items = (t && Array.isArray(t.timeline)) ? t.timeline : [];
      return items.filter(item => item && item.time && item.event);
    },
    patchArtifact() {
      return this.executionArtifacts.patch || null;
    },
    validationArtifact() {
      return this.executionArtifacts.validation || null;
    },
    reviewArtifact() {
      return this.executionArtifacts.review || null;
    },
    patchFiles() {
      const patch = this.patchArtifact;
      return (patch && Array.isArray(patch.files)) ? patch.files : [];
    },
    validationChecks() {
      const validation = this.validationArtifact;
      return (validation && Array.isArray(validation.checks)) ? validation.checks : [];
    },
    verdictToneClass() {
      const v = this.latestReview && this.latestReview.verdict;
      if (v === 'pass') return 'verdict-tone-pass';
      if (v === 'fail') return 'verdict-tone-fail';
      return 'verdict-tone-unknown';
    },
    taskContentText() {
      const task = this.task;
      const text = String((task && task.content) || '').trim();
      if (text) return task.content;
      return [
        '## 任务正文为空',
        '',
        '- 这个任务只有标题，没有保存正文。',
        '- 常见原因：早期版本的 `codepilot add` 占位通道遗留；新版已强制要求 content 模板合规。',
        '- 处理方式：按 `codepilot ai template --format json` 的 schema 补齐 `content`，或删除后重新导入。',
      ].join('\n');
    },
    taskContentLong() {
      const text = String(this.taskContentText || '');
      return text.length > 1800 || text.split(/\r?\n/).length > 36;
    },
    taskContentCollapsed() {
      return this.taskContentLong && !this.showFullTaskContent;
    },
  },
  watch: {
    taskId() {
      this.resetTaskDetailUi();
    },
  },
  methods: {
    resetTaskDetailUi() {
      this.expandedPhaseKeys = {};
      this.phaseRawLogs = {};
      this.phaseRawLoading = {};
      this.phaseRawErrors = {};
      this.showFullTaskContent = false;
    },
    isPhaseExpanded(phase) {
      if (!phase || !phase.key) return false;
      if (Object.prototype.hasOwnProperty.call(this.expandedPhaseKeys, phase.key)) {
        return !!this.expandedPhaseKeys[phase.key];
      }
      return !!phase.default_expanded;
    },
    togglePhase(phase) {
      if (!phase || !phase.key) return;
      const next = !this.isPhaseExpanded(phase);
      this.expandedPhaseKeys = { ...this.expandedPhaseKeys, [phase.key]: next };
    },
    phaseLogState(phase) {
      if (!phase || !phase.key) return null;
      return this.phaseRawLogs[phase.key] || null;
    },
    phaseLogText(phase) {
      if (!phase) return '';
      if (phase.active) return (this.s.taskLog && this.s.taskLog.text) || '';
      const state = this.phaseLogState(phase);
      return (state && state.text) || '';
    },
    phaseLogDone(phase) {
      if (!phase) return true;
      if (phase.active) return false;
      const state = this.phaseLogState(phase);
      return !!(state && state.done);
    },
    phaseLogTitle(phase) {
      const label = (phase && phase.label) || 'Phase';
      const filename = (phase && phase.raw_filename) || this.logFileName;
      return `${label} · ${filename || 'log'}`;
    },
    phaseStatusLabel(phase) {
      const status = String((phase && phase.status) || '');
      const labels = {
        done: '完成',
        failed: '失败',
        running: '运行中',
        unknown: '待确认',
      };
      return labels[status] || status || '-';
    },
    phaseLineCount(phase) {
      const text = this.phaseLogText(phase);
      return text ? text.split(/\r?\n/).length : 0;
    },
    formatBytes(bytes) {
      const n = Number(bytes);
      if (!Number.isFinite(n) || n <= 0) return '0 B';
      if (n < 1024) return `${Math.round(n)} B`;
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
      return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    },
    async loadPhaseLog(phase) {
      if (!phase || !phase.key || !this.task || !phase.raw_available) return;
      if (this.phaseRawLoading[phase.key]) return;
      const current = this.phaseRawLogs[phase.key] || { text: '', nextOffset: 0, done: false };
      if (current.done && current.text) return;
      this.phaseRawLoading = { ...this.phaseRawLoading, [phase.key]: true };
      this.phaseRawErrors = { ...this.phaseRawErrors, [phase.key]: '' };
      try {
        let state = { ...current };
        let guard = 64;
        while (guard-- > 0 && !state.done) {
          const offset = Number.isFinite(state.nextOffset) ? state.nextOffset : 0;
          const data = await CP.api.get(`/api/tasks/${this.task.id}/phase-logs/${encodeURIComponent(phase.key)}?offset=${offset}`);
          const chunk = (data && typeof data.text === 'string') ? data.text : '';
          const nextOffset = Number.isFinite(data && data.next_offset) ? data.next_offset : offset;
          state = {
            text: `${state.text || ''}${chunk}`,
            nextOffset,
            size: Number.isFinite(data && data.size) ? data.size : state.size,
            done: !!(data && data.done),
            source: (data && data.source) || phase.raw_source || '',
          };
          this.phaseRawLogs = { ...this.phaseRawLogs, [phase.key]: state };
          if (state.done || (!chunk && nextOffset <= offset)) break;
        }
      } catch (err) {
        const message = (err && err.message) ? err.message : '阶段日志加载失败';
        this.phaseRawErrors = { ...this.phaseRawErrors, [phase.key]: message };
      } finally {
        this.phaseRawLoading = { ...this.phaseRawLoading, [phase.key]: false };
      }
    },
    async action(id, act) {
      if (act === 'split') {
        const ok = await this.cp.confirm({
          title: '拆分任务',
          message: '将此任务关闭并拆成更小的新任务？',
          confirmText: '拆分',
          cancelText: '取消',
          tone: 'warning',
        });
        if (!ok) return;
      }
      this.cp.taskAction(id, act);
    },
    formatEta(seconds) {
      if (!seconds) return '';
      if (seconds < 60) return `${Math.round(seconds)}s`;
      if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
      const h = Math.floor(seconds / 3600);
      const m = Math.round((seconds % 3600) / 60);
      return m ? `${h}h${m}m` : `${h}h`;
    },
    artifactStatusLabel(item) {
      if (!item) return '-';
      const status = String(item.status || item.verdict || '').trim();
      return status || '-';
    },
    artifactTone(item) {
      const status = String((item && (item.status || item.verdict)) || '').toLowerCase();
      if (['pass', 'passed', 'captured'].includes(status)) return 'good';
      if (['fail', 'failed'].includes(status)) return 'bad';
      if (['empty', 'none', 'not_run'].includes(status)) return 'muted';
      return 'neutral';
    },
    formatTimelineEvent(item) {
      const labels = {
        created: 'Created',
        planned: 'Planned',
        claimed: 'Claimed',
        agent_started: 'Agent',
        diff_detected: 'Diff',
        validated: 'Validation',
        reviewed: 'Review',
        blocked: 'Blocked',
        done: 'Done',
        failed: 'Failed',
      };
      const key = String((item && item.event) || '').trim();
      return labels[key] || key || '-';
    },
  },
  template: `
    <div class="view">
      <div v-if="s.taskDetailLoading && !task" class="big-empty">任务加载中…</div>
      <div v-else-if="!task" class="big-empty">{{ s.taskDetailError || '暂无任务详情' }}</div>
      <section v-else class="card">
        <div class="card-head task-detail-head view-toolbar">
          <div class="row between gap-sm">
            <div class="min-w grow">
              <h3 class="detail-title">#{{ task.id }} {{ task.title }}</h3>
              <div class="chip-row mt-xs">
                <!-- Preflight-skipped tasks wear a yellow 预检跳过 chip
                     instead of the gray 待办, so the detail page matches
                     the sidebar / list-card warning tone. -->
                <cp-chip v-if="task.skip_reason" tone="warning">预检跳过</cp-chip>
                <cp-chip v-else :tone="$cp.toneClass(task.status)">{{ $cp.statusLabel(task.status) }}</cp-chip>
                <cp-chip>{{ task.priority }}</cp-chip>
                <cp-chip>{{ task.agent || '-' }}</cp-chip>
                <cp-chip>重试 {{ task.retry_count || 0 }}/{{ task.max_retries || 0 }}</cp-chip>
                <cp-chip v-if="task.eta_seconds">预计 ~{{ formatEta(task.eta_seconds) }}</cp-chip>
              </div>
            </div>
            <div class="row gap-xs">
              <button v-if="task.actions && task.actions.promote" class="btn btn-outline btn-sm"
                      :disabled="cp.isTaskPending(task.id)" @click="action(task.id, 'promote')">
                <span v-if="cp.taskPendingAction(task.id) === 'promote'" class="spinner"></span>插队 P0
              </button>
              <button v-if="task.actions && task.actions.retry" class="btn btn-warning btn-sm"
                      :disabled="cp.isTaskPending(task.id)" @click="action(task.id, 'retry')">
                <span v-if="cp.taskPendingAction(task.id) === 'retry'" class="spinner"></span>重试
              </button>
              <button v-if="canSplit" class="btn btn-outline btn-sm"
                      :disabled="cp.isTaskPending(task.id)" @click="action(task.id, 'split')" title="把这个任务重新拆成更小的几个任务">
                <span v-if="cp.taskPendingAction(task.id) === 'split'" class="spinner"></span>拆分
              </button>
              <button v-if="task.actions && task.actions.stop" class="btn btn-danger btn-sm"
                      :disabled="cp.isTaskPending(task.id)" @click="action(task.id, 'stop')">
                <span v-if="cp.taskPendingAction(task.id) === 'stop'" class="spinner"></span>停止
              </button>
            </div>
          </div>
        </div>
        <div class="card-body task-detail task-detail-body">
          <div class="task-phase-progress task-phase-progress-detail" :class="'tone-' + $cp.taskPhaseProgress(task).tone">
            <div class="task-phase-progress-head">
              <div class="task-phase-current">
                <span class="phase-pulse" v-if="task.status === 'in_progress'"></span>
                {{ $cp.taskPhaseProgress(task).currentLabel }}
              </div>
              <div class="task-phase-next">{{ $cp.taskPhaseProgress(task).nextLabel }}</div>
            </div>
            <div class="task-phase-steps">
              <span v-for="step in $cp.taskPhaseProgress(task).steps"
                    :key="step.key"
                    class="task-phase-step"
                    :class="'state-' + step.state">
                <span class="task-phase-dot"></span>
                <span>{{ step.label }}</span>
              </span>
            </div>
          </div>
          <div class="task-kv-grid detail-grid">
            <div class="task-kv-item">
              <span class="task-kv-key">项目</span>
              <span class="task-kv-value">{{ task.project || '-' }}</span>
            </div>
            <div class="task-kv-item">
              <span class="task-kv-key">阶段</span>
              <span class="task-kv-value">{{ task.phase || '-' }}</span>
            </div>
            <div class="task-kv-item">
              <span class="task-kv-key">创建时间</span>
              <span class="task-kv-value">{{ $cp.fmtTime(task.created_at) }}</span>
            </div>
            <div class="task-kv-item">
              <span class="task-kv-key">开始时间</span>
              <span class="task-kv-value">{{ $cp.fmtTime(task.started_at) }}</span>
            </div>
            <div class="task-kv-item">
              <span class="task-kv-key">完成时间</span>
              <span class="task-kv-value">{{ $cp.fmtTime(task.completed_at) }}</span>
            </div>
            <div class="task-kv-item span-2">
              <span class="task-kv-key">依赖任务</span>
              <span class="task-kv-value task-kv-value-wrap">
              <template v-if="task.depends_on && task.depends_on.length">
                <cp-chip v-for="id in task.depends_on" :key="id">#{{ id }}</cp-chip>
              </template>
              <span v-else class="muted">无</span>
              </span>
            </div>
            <div class="task-kv-item span-2">
              <span class="task-kv-key">工作目录</span>
              <code class="task-inline-code" :title="task.project_path || '-'">{{ task.project_path || '-' }}</code>
            </div>
            <div class="task-kv-item span-2">
              <span class="task-kv-key">日志文件</span>
              <code class="task-inline-code" :title="task.current_log_path || '-'">{{ task.current_log_path || '-' }}</code>
            </div>
          </div>
          <div class="block task-timeline-block">
            <div class="block-label">任务时间线</div>
            <div v-if="timelineEvents.length" class="timeline-event-list">
              <div v-for="(item, idx) in timelineEvents" :key="idx" class="timeline-event-row">
                <span class="timeline-event-dot" :class="'event-' + (item.event || 'event')"></span>
                <span class="timeline-event-time">{{ $cp.fmtTime(item.time) || item.time || '-' }}</span>
                <span class="timeline-event-kind">{{ formatTimelineEvent(item) }}</span>
                <span class="timeline-event-message">{{ item.message || '-' }}</span>
                <code v-if="item.artifact_path" class="timeline-event-artifact" :title="item.artifact_path">{{ item.artifact_path }}</code>
              </div>
            </div>
            <div v-else class="tiny muted">暂无 timeline 事件</div>
          </div>
          <div v-if="task.skip_reason" class="block warning">
            <div class="block-label">预检跳过（未消耗重试次数，下一轮会自动重试）</div>
            <cp-markdown :text="task.skip_reason"></cp-markdown>
          </div>
          <div v-else-if="task.error_message" class="block danger">
            <div class="block-label">失败原因</div>
            <cp-markdown :text="task.error_message"></cp-markdown>
          </div>
          <div class="block task-content-block" :class="{ 'is-collapsed': taskContentCollapsed }">
            <div class="block-label">任务说明</div>
            <div class="task-content-md">
              <cp-markdown :text="taskContentText"></cp-markdown>
            </div>
            <button v-if="taskContentLong"
                    class="btn btn-outline btn-sm task-content-toggle"
                    @click="showFullTaskContent = !showFullTaskContent">
              {{ showFullTaskContent ? '收起任务说明' : '展开完整任务说明' }}
            </button>
          </div>
          <div v-if="task.status === 'in_progress'" class="task-run-status">
            <span class="task-run-status-label">{{ runStatusLabel }}</span>
            <span class="task-run-status-elapsed">已运行 {{ runElapsedLabel }}</span>
            <span v-if="isRunIdle" class="task-run-status-idle">
              <span class="idle-dot"></span>等待响应 · 静默 {{ runIdleSeconds }}s
            </span>
          </div>
          <div class="block task-process-block">
            <div class="task-process-head">
              <div>
                <div class="block-label">执行过程</div>
                <div class="tiny muted">按阶段查看 builder/reviewer 过程，原始日志按需加载</div>
              </div>
              <div class="chip-row task-process-stats">
                <cp-chip tiny tone="info">{{ processPhases.length }} 阶段</cp-chip>
              </div>
            </div>
            <div v-if="processPhases.length" class="task-phase-log-list">
              <section v-for="phase in processPhases"
                       :key="phase.key"
                       class="task-phase-log"
                       :class="'status-' + (phase.status || 'unknown')">
                <button type="button" class="task-phase-log-head" @click="togglePhase(phase)">
                  <span class="task-phase-caret">{{ isPhaseExpanded(phase) ? 'v' : '>' }}</span>
                  <span class="task-phase-run-indicator">
                    <span v-if="phase.active" class="phase-active-spinner"></span>
                  </span>
                  <span class="task-phase-log-title">{{ phase.label || phase.phase || 'Phase' }}</span>
                  <span class="task-phase-log-meta">{{ phase.agent || '-' }}</span>
                  <span class="task-phase-log-status">{{ phaseStatusLabel(phase) }}</span>
                  <span v-if="phase.exit_code !== null && phase.exit_code !== undefined" class="task-phase-log-meta">exit {{ phase.exit_code }}</span>
                  <span v-if="phase.duration !== null && phase.duration !== undefined" class="task-phase-log-meta">{{ phase.duration }}s</span>
                  <span v-if="phase.raw_available" class="task-phase-log-meta">{{ formatBytes(phase.raw_size) }}</span>
                </button>
                <div v-if="isPhaseExpanded(phase)" class="task-phase-log-body">
                  <div class="task-phase-log-summary">{{ phase.summary || '-' }}</div>
                  <div v-if="phase.active && !phaseLogText(phase)" class="task-phase-log-loading">
                    <span class="phase-active-spinner"></span>等待实时输出
                  </div>
                  <div v-else-if="!phase.active && !phaseLogText(phase)" class="task-phase-log-actions">
                    <button v-if="phase.raw_available"
                            class="btn btn-outline btn-sm"
                            :disabled="phaseRawLoading[phase.key]"
                            @click.stop="loadPhaseLog(phase)">
                      <span v-if="phaseRawLoading[phase.key]" class="spinner tiny-spinner"></span>
                      加载原始日志
                    </button>
                    <span v-else class="tiny muted">没有可加载的原始日志</span>
                  </div>
                  <div v-if="phaseRawErrors[phase.key]" class="tiny danger-text">{{ phaseRawErrors[phase.key] }}</div>
                  <cp-agent-log v-if="phaseLogText(phase)"
                                :text="phaseLogText(phase)"
                                :title="phaseLogTitle(phase)"
                                :done="phaseLogDone(phase)"
                                tall
                                :follow="phase.active"></cp-agent-log>
                  <div v-if="phaseLogText(phase)" class="tiny muted task-phase-log-foot">
                    {{ phaseLineCount(phase) }} 行
                  </div>
                </div>
              </section>
            </div>
            <div v-else class="tiny muted">暂无阶段日志</div>
          </div>
          <div v-if="hasExecutionArtifacts" class="block artifact-review-block">
            <div class="block-label">Diff 审查</div>
            <div class="artifact-review-grid">
              <div class="artifact-panel" v-if="patchArtifact">
                <div class="artifact-panel-head">
                  <span>Patch</span>
                  <span class="artifact-status" :class="'artifact-' + artifactTone(patchArtifact)">
                    {{ artifactStatusLabel(patchArtifact) }}
                  </span>
                </div>
                <div class="artifact-summary">{{ patchArtifact.summary || '-' }}</div>
                <div v-if="patchFiles.length" class="artifact-file-list">
                  <div v-for="file in patchFiles.slice(0, 8)" :key="file.path" class="artifact-file-row">
                    <span class="artifact-file-status">{{ file.status || '-' }}</span>
                    <code>{{ file.path }}</code>
                  </div>
                  <div v-if="patchArtifact.truncated_files" class="tiny muted">
                    +{{ patchArtifact.truncated_files }} more
                  </div>
                </div>
              </div>
              <div class="artifact-panel" v-if="validationArtifact">
                <div class="artifact-panel-head">
                  <span>Validation</span>
                  <span class="artifact-status" :class="'artifact-' + artifactTone(validationArtifact)">
                    {{ artifactStatusLabel(validationArtifact) }}
                  </span>
                </div>
                <div class="artifact-summary">{{ validationArtifact.summary || '-' }}</div>
                <div v-if="validationChecks.length" class="artifact-check-list">
                  <div v-for="(check, idx) in validationChecks.slice(0, 4)" :key="idx" class="artifact-check-row">
                    <span class="artifact-check-exit" :class="{ ok: check.ok === true, fail: check.ok === false }">
                      {{ check.exit_code === null || check.exit_code === undefined ? '-' : check.exit_code }}
                    </span>
                    <code>{{ check.command || '-' }}</code>
                  </div>
                </div>
              </div>
              <div class="artifact-panel" v-if="reviewArtifact">
                <div class="artifact-panel-head">
                  <span>Review</span>
                  <span class="artifact-status" :class="'artifact-' + artifactTone(reviewArtifact)">
                    {{ artifactStatusLabel(reviewArtifact) }}
                  </span>
                </div>
                <div class="artifact-summary">{{ reviewArtifact.summary || '-' }}</div>
                <div v-if="reviewArtifact.blockers && reviewArtifact.blockers.length" class="artifact-mini-list">
                  <div v-for="(item, idx) in reviewArtifact.blockers.slice(0, 3)" :key="idx">{{ item }}</div>
                </div>
              </div>
            </div>
          </div>
          <div v-if="task.delivery_record" class="block">
            <div class="block-label">交付记录</div>
            <cp-markdown :text="task.delivery_record"></cp-markdown>
          </div>
          <div v-if="latestReview" class="block reviewer-verdict" :class="verdictToneClass">
            <div class="block-label">
              最新审查结论
              <span class="reviewer-verdict-meta">
                {{ latestReview.agent || 'reviewer' }}
                <span v-if="latestReview.source" class="reviewer-verdict-source">· source={{ latestReview.source }}</span>
              </span>
            </div>
            <div class="reviewer-verdict-badge" :class="'verdict-' + (latestReview.verdict || 'unknown')">
              VERDICT: {{ (latestReview.verdict || 'unknown').toUpperCase() }}
            </div>
            <table v-if="latestReview.ac_checks && latestReview.ac_checks.length" class="ac-checks-table">
              <thead>
                <tr><th>AC</th><th>状态</th><th>说明</th></tr>
              </thead>
              <tbody>
                <tr v-for="(ac, idx) in latestReview.ac_checks" :key="idx" :class="'ac-row ac-' + ((ac.status || '').toLowerCase())">
                  <td>{{ ac.id || '-' }}</td>
                  <td><span class="ac-status-chip" :class="'ac-status-' + ((ac.status || '').toLowerCase())">{{ ac.status || '-' }}</span></td>
                  <td>{{ ac.reason || '-' }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="latestReview.blockers && latestReview.blockers.length" class="reviewer-block-section">
              <div class="reviewer-block-sublabel">阻塞点</div>
              <ul>
                <li v-for="(item, idx) in latestReview.blockers" :key="'b'+idx">{{ item }}</li>
              </ul>
            </div>
            <div v-if="latestReview.advisory && latestReview.advisory.length" class="reviewer-block-section">
              <div class="reviewer-block-sublabel">非阻塞观察</div>
              <ul>
                <li v-for="(item, idx) in latestReview.advisory" :key="'a'+idx">{{ item }}</li>
              </ul>
            </div>
          </div>
        </div>
      </section>
    </div>
  `,
});
