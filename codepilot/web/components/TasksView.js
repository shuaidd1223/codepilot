/* All tasks grouped by status for current project. */
/* global Vue, CP */
CP.Components.TasksView = Vue.defineComponent({
  name: 'CpTasksView',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    running() { return this.s.tasks.filter(t => t.status === 'in_progress'); },
    backlog() { return this.s.tasks.filter(t => t.status === 'backlog'); },
    failed() { return this.s.tasks.filter(t => t.status === 'failed' || t.status === 'cancelled'); },
    done() { return this.s.tasks.filter(t => t.status === 'done'); },
  },
  template: `
    <div class="view">
      <div class="view-toolbar">
        <h2 class="view-title">任务</h2>
        <div class="muted tiny">共 {{ s.tasks.length }} 个</div>
      </div>
      <cp-task-section title="进行中" :tasks="running" empty="当前没有运行中的任务"></cp-task-section>
      <cp-task-section title="待办 / 可重试" :tasks="backlog" empty="当前 backlog 为空"></cp-task-section>
      <cp-task-section title="失败 / 已取消" :tasks="failed" empty="当前没有失败或取消的任务"></cp-task-section>
      <cp-task-section title="最近完成" :tasks="done" empty="还没有已完成任务"></cp-task-section>
    </div>
  `,
});
