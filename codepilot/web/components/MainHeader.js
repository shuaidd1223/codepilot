/* Top bar: breadcrumb + refresh + auto-toggle. */
/* global Vue, CP */
CP.Components.MainHeader = Vue.defineComponent({
  name: 'CpMainHeader',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    currentProject() { return this.cp.currentProject; },
    breadcrumb() {
      const nav = this.s.nav;
      const parts = [];
      if (nav.project) parts.push({ label: nav.project, click: () => this.cp.selectProject(nav.project) });
      if (nav.view && nav.view !== 'overview') {
        const label = { sessions: '会话', tasks: '任务', jobs: '需求', session: '会话', task: '任务', job: '需求' }[nav.view] || nav.view;
        parts.push({ label });
        if (nav.id) parts.push({ label: '#' + nav.id });
      }
      return parts;
    },
  },
  template: `
    <header class="main-header">
      <div class="min-w">
        <div class="breadcrumb">
          <template v-if="breadcrumb.length">
            <template v-for="(b, i) in breadcrumb" :key="i">
              <span v-if="i > 0" class="crumb-sep">/</span>
              <button v-if="b.click" class="crumb-link" @click="b.click">{{ b.label }}</button>
              <span v-else class="crumb-text">{{ b.label }}</span>
            </template>
          </template>
          <span v-else class="crumb-text muted">请选择一个项目</span>
        </div>
        <div v-if="currentProject" class="muted tiny truncate">{{ currentProject.path }}</div>
      </div>
      <div class="row gap-sm">
        <button class="btn btn-outline" @click="cp.loadDashboard()" :disabled="s.loading">
          <span v-if="s.loading" class="spinner"></span>
          <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>
          刷新
        </button>
        <button class="btn" :class="s.autoRefresh ? 'btn-primary' : 'btn-outline'" @click="cp.toggleAuto()">
          <span class="dot" :class="{on: s.autoRefresh}"></span>
          自动刷新 {{ s.autoRefresh ? '开' : '关' }}
        </button>
      </div>
    </header>
  `,
});
