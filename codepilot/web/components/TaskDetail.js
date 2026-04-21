/* Task detail view. */
/* global Vue, CP */
CP.Components.TaskDetail = Vue.defineComponent({
  name: 'CpTaskDetail',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    task() { return this.s.taskDetail; },
    canSplit() {
      const t = this.task;
      if (!t) return false;
      /* Offer split on failed / attention / backlog tasks where the user
       * likely regrets the scope. Not on in-progress / done. */
      return ['failed', 'cancelled', 'backlog'].includes(t.status);
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
        <div class="card-head">
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
        <div class="card-body task-detail">
          <div class="kv-grid">
            <div><b>项目:</b> {{ task.project }}</div>
            <div><b>阶段:</b> {{ task.phase || '-' }}</div>
            <div><b>开始:</b> {{ $cp.fmtTime(task.started_at) }}</div>
            <div><b>完成:</b> {{ $cp.fmtTime(task.completed_at) }}</div>
            <div class="full"><b>依赖:</b>
              <template v-if="task.depends_on && task.depends_on.length">
                <cp-chip v-for="id in task.depends_on" :key="id">#{{ id }}</cp-chip>
              </template>
              <span v-else> 无</span>
            </div>
            <div class="full truncate"><b>路径:</b> {{ task.project_path || '-' }}</div>
            <div class="full truncate"><b>日志文件:</b> {{ task.current_log_path || '-' }}</div>
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
            <div class="block-label">任务内容</div>
            <cp-markdown :text="task.content || '暂无任务内容'"></cp-markdown>
          </div>
          <div class="block">
            <div class="block-label">实时日志</div>
            <cp-agent-log
              :text="s.taskLog.text || ''"
              :title="(task.agent || 'agent') + ' · ' + (task.current_log_path ? task.current_log_path.split(/[\\\\/]/).pop() : 'log')"
              :done="s.taskLog.done"
              tall follow></cp-agent-log>
          </div>
          <div v-if="task.delivery_record" class="block">
            <div class="block-label">交付记录</div>
            <cp-markdown :text="task.delivery_record"></cp-markdown>
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
