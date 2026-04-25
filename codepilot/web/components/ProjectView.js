/* Default view when a project is selected — quick input + composer + metrics. */
/* global Vue, CP */
CP.Components.ProjectView = Vue.defineComponent({
  name: 'CpProjectView',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    currentProject() { return this.cp.currentProject; },
    runningTasks() { return this.s.tasks.filter(t => t.status === 'in_progress'); },
    taskService() { return (this.currentProject && this.currentProject.services && this.currentProject.services.tasks) || {}; },
    inspectService() { return (this.currentProject && this.currentProject.services && this.currentProject.services.inspect) || {}; },
  },
  methods: {
    servicePending(service, action) {
      return this.s.servicePending === service + ':' + action;
    },
  },
  template: `
    <div class="view">
      <section class="card">
        <div class="card-head row between">
          <div>
            <h3>项目服务</h3>
            <p class="muted">按项目独立启动和停止任务执行、巡检进程</p>
          </div>
          <cp-chip :tone="taskService.running || inspectService.running ? 'success' : 'neutral'">
            {{ taskService.running || inspectService.running ? '服务运行中' : '服务未运行' }}
          </cp-chip>
        </div>
        <div class="service-grid">
          <div class="service-row">
            <div class="service-main">
              <div class="service-title">任务执行</div>
              <div class="muted tiny">
                {{ taskService.running ? (taskService.stopping ? ('停止中，PID ' + taskService.pid) : ('PID ' + taskService.pid)) : '未运行' }}
              </div>
            </div>
            <div class="row gap-xs">
              <button class="btn btn-primary btn-sm" @click="cp.projectService('tasks', 'start')" :disabled="taskService.running || !!s.servicePending">
                <span v-if="servicePending('tasks', 'start')" class="spinner"></span>
                启动
              </button>
              <button class="btn btn-danger-outline btn-sm" @click="cp.projectService('tasks', 'stop')" :disabled="!taskService.running || taskService.stopping || !!s.servicePending">
                <span v-if="servicePending('tasks', 'stop')" class="spinner"></span>
                停止轮询
              </button>
            </div>
          </div>
          <div class="service-row">
            <div class="service-main">
              <div class="service-title">项目巡检</div>
              <div class="muted tiny">
                {{ inspectService.running ? ('PID ' + inspectService.pid) : '未运行' }}
              </div>
            </div>
            <div class="row gap-xs">
              <button class="btn btn-primary btn-sm" @click="cp.projectService('inspect', 'start')" :disabled="inspectService.running || !!s.servicePending">
                <span v-if="servicePending('inspect', 'start')" class="spinner"></span>
                启动
              </button>
              <button class="btn btn-danger-outline btn-sm" @click="cp.projectService('inspect', 'stop')" :disabled="!inspectService.running || !!s.servicePending">
                <span v-if="servicePending('inspect', 'stop')" class="spinner"></span>
                停止
              </button>
            </div>
          </div>
        </div>
      </section>

      <cp-goal-input></cp-goal-input>

      <div class="grid grid-2">
        <cp-composer></cp-composer>
        <cp-metrics-panel></cp-metrics-panel>
      </div>

      <cp-task-batch-import></cp-task-batch-import>

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
              <div class="task-item-meta">
                <span v-if="x.created_at">创建: {{ $cp.fmtTime(x.created_at) }}</span>
              </div>
              <div class="task-item-preview">{{ x.runtime || x.latest || '暂无详细信息' }}</div>
            </div>
          </article>
        </div>
      </section>
    </div>
  `,
});
