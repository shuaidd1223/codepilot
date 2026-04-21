/* Single job detail. */
/* global Vue, CP */
CP.Components.JobDetail = Vue.defineComponent({
  name: 'CpJobDetail',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    job() { return this.cp.currentJob; },
    /* Live progress events pushed via SSE. Filtered down to events for
     * any of this job's tasks plus "no task" events (planner/recon signals
     * emitted before a task ID exists). */
    liveEvents() {
      if (!this.job) return [];
      return this.cp.liveEventsForJob(this.job.id);
    },
  },
  template: `
    <div class="view">
      <div v-if="!job" class="big-empty">需求不存在或已过期</div>
      <section v-else class="card">
        <div class="card-head">
          <h3>
            <span v-if="$cp.isJobActive(job)" class="spinner"></span>
            #{{ job.id }} {{ job.title }}
          </h3>
          <p class="muted">planner: {{ job.planner || '-' }} · agent: {{ job.agent || 'auto' }}</p>
        </div>
        <div class="card-body" style="display:flex;flex-direction:column;gap:12px">
          <div class="chip-row">
            <cp-chip :tone="$cp.toneClass(job.status)">{{ $cp.statusLabel(job.status) }}</cp-chip>
            <cp-chip :tone="$cp.toneClass(job.phase)">{{ $cp.phaseLabel(job.phase) || job.phase }}</cp-chip>
          </div>
          <div class="kv-grid">
            <div><b>创建:</b> {{ $cp.fmtTime(job.created_at) }}</div>
            <div><b>更新:</b> {{ $cp.fmtTime(job.updated_at) }}</div>
            <div><b>结束:</b> {{ $cp.fmtTime(job.finished_at) }}</div>
            <div v-if="job.task_ids && job.task_ids.length" class="full">
              <b>关联任务:</b>
              <button v-for="id in job.task_ids" :key="id" class="chip primary" @click="cp.selectTask(s.nav.project, id)">#{{ id }}</button>
            </div>
          </div>
          <div v-if="job.summary" class="block">
            <div class="block-label">摘要</div>
            <cp-markdown :text="job.summary"></cp-markdown>
          </div>
          <div v-if="job.error" class="block danger">
            <div class="block-label">错误</div>
            <cp-markdown :text="job.error"></cp-markdown>
          </div>
          <div v-if="job.log && job.log.length" class="block">
            <div class="block-label">日志</div>
            <cp-code-block :text="job.log.join('\\n')" tall follow></cp-code-block>
          </div>
          <div v-if="liveEvents.length" class="block">
            <div class="block-label">
              实时进度
              <span v-if="$cp.isJobActive(job)" class="spinner" style="margin-left:6px"></span>
            </div>
            <cp-live-log :events="liveEvents" tall follow></cp-live-log>
          </div>
        </div>
      </section>
    </div>
  `,
});
