/* Right pane — routes to the right view based on nav. */
/* global Vue, CP */
CP.Components.ContentPane = Vue.defineComponent({
  name: 'CpContentPane',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    view() { return this.s.nav.view; },
  },
  template: `
    <div class="content-pane">
      <div v-if="!s.nav.project" class="big-empty">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <div>从左侧选择一个项目开始</div>
      </div>
      <cp-project-view v-else-if="view === 'overview'"></cp-project-view>
      <cp-sessions-view v-else-if="view === 'sessions'"></cp-sessions-view>
      <cp-tasks-view v-else-if="view === 'tasks'"></cp-tasks-view>
      <cp-jobs-view v-else-if="view === 'jobs'"></cp-jobs-view>
      <cp-chat-view v-else-if="view === 'session'"></cp-chat-view>
      <cp-task-detail v-else-if="view === 'task'"></cp-task-detail>
      <cp-job-detail v-else-if="view === 'job'"></cp-job-detail>
      <div v-else class="big-empty">未知视图：{{ view }}</div>
    </div>
  `,
});
