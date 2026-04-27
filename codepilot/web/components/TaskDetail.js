/* Task detail view. */
/* global Vue, CP */
CP.Components.TaskDetail = Vue.defineComponent({
  name: 'CpTaskDetail',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    task() { return this.s.taskDetail; },
    logText() {
      return (this.s.taskLog && this.s.taskLog.text) || '';
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
  },
  methods: {
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
  },
  template: `
    <div class="view">
      <div v-if="s.taskDetailLoading && !task" class="big-empty">任务加载中…</div>
      <div v-else-if="!task" class="big-empty">{{ s.taskDetailError || '暂无任务详情' }}</div>
      <section v-else class="card">
        <div class="card-head task-detail-head">
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
            <div class="task-phase-track">
              <span class="task-phase-fill" :style="{ width: $cp.taskPhaseProgress(task).percent + '%' }"></span>
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
          <div class="task-kv-grid">
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
          <div v-if="task.skip_reason" class="block warning">
            <div class="block-label">预检跳过（未消耗重试次数，下一轮会自动重试）</div>
            <cp-markdown :text="task.skip_reason"></cp-markdown>
          </div>
          <div v-else-if="task.error_message" class="block danger">
            <div class="block-label">失败原因</div>
            <cp-markdown :text="task.error_message"></cp-markdown>
          </div>
          <div class="block">
            <div class="block-label">任务说明</div>
            <cp-markdown :text="taskContentText"></cp-markdown>
          </div>
          <div class="block block-log-stream">
            <div class="task-log-head">
              <div class="min-w grow">
                <div class="block-label">实时日志</div>
                <div class="tiny muted">{{ logSummaryText }}</div>
              </div>
              <div class="chip-row task-log-stats">
                <cp-chip tiny tone="info">{{ logStats.lines }} 行</cp-chip>
                <cp-chip v-if="logStats.tools" tiny tone="primary">工具 {{ logStats.tools }}</cp-chip>
                <cp-chip v-if="logStats.diffs" tiny tone="success">Diff {{ logStats.diffs }}</cp-chip>
                <cp-chip v-if="logStats.warns" tiny tone="warning">关注 {{ logStats.warns }}</cp-chip>
              </div>
            </div>
            <cp-agent-log
              :text="logText"
              :title="logTitle"
              :done="s.taskLog.done"
              tall follow></cp-agent-log>
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
          <div v-if="task.logs && task.logs.length" class="block">
            <div class="block-label">阶段日志摘要</div>
            <cp-markdown :text="$cp.formatLogs(task.logs)"></cp-markdown>
          </div>
        </div>
      </section>
    </div>
  `,
});
