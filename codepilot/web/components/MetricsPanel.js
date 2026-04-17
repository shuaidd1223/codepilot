/* Project metrics card. */
/* global Vue, CP */
CP.Components.MetricsPanel = Vue.defineComponent({
  name: 'CpMetricsPanel',
  inject: ['cp'],
  computed: {
    currentProject() { return this.cp.currentProject; },
    metrics() { return this.cp.metrics; },
  },
  template: `
    <section class="card">
      <div class="card-head">
        <h3>项目状态</h3>
        <p class="muted">{{ currentProject ? ('活动摘要: ' + (currentProject.active_summary || '无')) : '-' }}</p>
      </div>
      <div class="metrics">
        <div v-for="m in metrics" :key="m.label" class="metric" :class="m.tone">
          <div class="metric-label">{{ m.label }}</div>
          <div class="metric-value">{{ m.value }}</div>
        </div>
      </div>
    </section>
  `,
});
