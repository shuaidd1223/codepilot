/* Task detail view. */
/* global Vue, CP */
CP.Components.TaskDetail = Vue.defineComponent({
  name: 'CpTaskDetail',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    task() { return this.s.taskDetail; },
  },
  methods: {
    action(id, act) { this.cp.taskAction(id, act); },
  },
  template: `
    <div class="view">
      <div v-if="!task" class="big-empty">任务加载中…</div>
      <section v-else class="card">
        <div class="card-head">
          <div class="row between gap-sm">
            <div class="min-w grow">
              <h3 class="detail-title">#{{ task.id }} {{ task.title }}</h3>
              <div class="chip-row mt-xs">
                <cp-chip :tone="$cp.toneClass(task.status)">{{ $cp.statusLabel(task.status) }}</cp-chip>
                <cp-chip>{{ task.priority }}</cp-chip>
                <cp-chip>{{ task.agent || '-' }}</cp-chip>
                <cp-chip>重试 {{ task.retry_count || 0 }}/{{ task.max_retries || 0 }}</cp-chip>
              </div>
            </div>
            <div class="row gap-xs">
              <button v-if="task.actions && task.actions.promote" class="btn btn-outline btn-sm" @click="action(task.id, 'promote')">插队 P0</button>
              <button v-if="task.actions && task.actions.retry" class="btn btn-warning btn-sm" @click="action(task.id, 'retry')">重试</button>
              <button v-if="task.actions && task.actions.stop" class="btn btn-danger btn-sm" @click="action(task.id, 'stop')">停止</button>
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
          <div v-if="task.error_message" class="block danger">
            <div class="block-label">失败原因</div>
            <pre class="code">{{ task.error_message }}</pre>
          </div>
          <div class="block">
            <div class="block-label">任务内容</div>
            <pre class="code">{{ task.content || '暂无任务内容' }}</pre>
          </div>
          <div class="block">
            <div class="block-label">实时日志 / 最近输出</div>
            <pre class="code tall">{{ task.log_text || '还没有可显示的日志' }}</pre>
          </div>
          <div v-if="task.delivery_record" class="block">
            <div class="block-label">交付记录</div>
            <pre class="code">{{ task.delivery_record }}</pre>
          </div>
          <div v-if="task.logs && task.logs.length" class="block">
            <div class="block-label">阶段日志摘要</div>
            <pre class="code">{{ $cp.formatLogs(task.logs) }}</pre>
          </div>
        </div>
      </section>
    </div>
  `,
});
