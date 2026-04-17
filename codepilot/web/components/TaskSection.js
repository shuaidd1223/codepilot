/* Reusable task list section. */
/* global Vue, CP */
CP.Components.TaskSection = Vue.defineComponent({
  name: 'CpTaskSection',
  inject: ['cp'],
  props: {
    title: { type: String, required: true },
    tasks: { type: Array, required: true },
    empty: { type: String, default: '暂无任务' },
  },
  methods: {
    pick(id) { this.cp.selectTask(this.cp.state.nav.project, id); },
    action(id, act) { this.cp.taskAction(id, act); },
  },
  template: `
    <section class="card">
      <div class="task-section-head">
        <h3>{{ title }}</h3>
        <span class="muted tiny">{{ tasks.length }}</span>
      </div>
      <div v-if="!tasks.length" class="empty pad">{{ empty }}</div>
      <div v-else class="task-list">
        <article v-for="x in tasks" :key="x.id" class="task-item" @click="pick(x.id)">
          <div class="task-item-body">
            <div class="task-item-title">#{{ x.id }} {{ x.title }}</div>
            <div class="chip-row">
              <cp-chip :tone="$cp.toneClass(x.status)">{{ $cp.statusLabel(x.status) }}</cp-chip>
              <cp-chip>{{ x.priority }}</cp-chip>
              <cp-chip>{{ x.agent || '-' }}</cp-chip>
              <cp-chip>重试 {{ x.retry_count || 0 }}/{{ x.max_retries || 0 }}</cp-chip>
              <cp-chip v-if="x.source === 'auto-inspect'" tone="warning">巡检</cp-chip>
            </div>
            <div class="task-item-meta">
              <span>阶段: {{ x.phase || '-' }}</span>
              <span v-if="x.started_at">开始: {{ $cp.fmtTime(x.started_at) }}</span>
              <span v-if="x.completed_at">完成: {{ $cp.fmtTime(x.completed_at) }}</span>
            </div>
            <div class="task-item-preview">{{ x.runtime || x.error_message || x.latest || '暂无详细信息' }}</div>
          </div>
          <div class="task-item-actions" @click.stop>
            <button v-if="x.actions.promote" class="btn btn-outline btn-sm" @click="action(x.id, 'promote')">插队</button>
            <button v-if="x.actions.retry" class="btn btn-warning btn-sm" @click="action(x.id, 'retry')">重试</button>
            <button v-if="x.actions.stop" class="btn btn-danger btn-sm" @click="action(x.id, 'stop')">停止</button>
          </div>
        </article>
      </div>
    </section>
  `,
});
