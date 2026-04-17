/* List all jobs (requirements) for current project. */
/* global Vue, CP */
CP.Components.JobsView = Vue.defineComponent({
  name: 'CpJobsView',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    jobs() { return this.s.jobs || []; },
  },
  template: `
    <div class="view">
      <div>
        <h2 class="view-title">需求</h2>
        <div class="muted tiny">由用户自然语言拆分成的需求（最多保留最近若干条）</div>
      </div>
      <div v-if="!jobs.length" class="empty pad">还没有需求，从项目概览提交一条试试</div>
      <div v-else class="cards-grid">
        <button v-for="j in jobs" :key="j.id" class="item-card" @click="cp.selectJob(s.nav.project, j.id)">
          <div class="row between">
            <strong class="truncate">
              <span v-if="$cp.isJobActive(j)" class="spinner"></span>
              #{{ j.id }} {{ j.title }}
            </strong>
            <cp-chip :tone="$cp.toneClass(j.status)">{{ $cp.phaseLabel(j.phase) || j.status }}</cp-chip>
          </div>
          <div class="muted tiny">planner: {{ j.planner || '-' }} · agent: {{ j.agent || 'auto' }}</div>
          <div v-if="j.summary || j.error" class="tiny">{{ j.summary || j.error }}</div>
        </button>
      </div>
    </div>
  `,
});
