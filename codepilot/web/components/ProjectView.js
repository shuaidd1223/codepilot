/* Default view when a project is selected — quick input + composer + metrics. */
/* global Vue, CP */
CP.Components.ProjectView = Vue.defineComponent({
  name: 'CpProjectView',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    currentProject() { return this.cp.currentProject; },
    runningTasks() { return this.s.tasks.filter(t => t.status === 'in_progress'); },
  },
  template: `
    <div class="view">
      <cp-goal-input></cp-goal-input>

      <div class="grid grid-2">
        <cp-composer></cp-composer>
        <cp-metrics-panel></cp-metrics-panel>
      </div>

      <section v-if="runningTasks.length" class="card">
        <div class="card-head">
          <h3>当前运行中</h3>
          <p class="muted">点击任务跳到详情</p>
        </div>
        <div class="task-list">
          <article v-for="x in runningTasks" :key="x.id" class="task-item" @click="cp.selectTask(s.nav.project, x.id)">
            <div class="task-item-body">
              <div class="task-item-title">#{{ x.id }} {{ x.title }}</div>
              <div class="chip-row">
                <cp-chip :tone="$cp.toneClass(x.status)">{{ $cp.statusLabel(x.status) }}</cp-chip>
                <cp-chip>{{ x.priority }}</cp-chip>
                <cp-chip>{{ x.agent || '-' }}</cp-chip>
              </div>
              <div class="task-item-preview">{{ x.runtime || x.latest || '暂无详细信息' }}</div>
            </div>
          </article>
        </div>
      </section>
    </div>
  `,
});
