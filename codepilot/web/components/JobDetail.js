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
    canCancel() {
      return this.job && this.$cp.isJobActive(this.job) && !this.job.cancel_requested;
    },
    canRetry() {
      return this.job && !this.$cp.isJobActive(this.job);
    },
    cancelPending() {
      return this.job && this.cp.isActionPending(`${this.cp.ACTION_KEYS.JOB_ACTION}:${this.job.id}:cancel`);
    },
    retryPending() {
      return this.job && this.cp.isActionPending(`${this.cp.ACTION_KEYS.JOB_ACTION}:${this.job.id}:retry`);
    },
  },
  methods: {
    cancelJob() {
      if (this.job) this.cp.jobAction(this.job, 'cancel');
    },
    retryJob() {
      if (this.job) this.cp.jobAction(this.job, 'retry');
    },
  },
  template: `
    <div class="view">
      <div v-if="!job" class="big-empty">需求不存在或已过期</div>
      <section v-else class="card">
        <div class="card-head task-detail-head">
          <h3 class="detail-title">
            <span v-if="$cp.isJobActive(job)" class="spinner" style="margin-right:6px"></span>
            #{{ job.id }} {{ job.title }}
          </h3>
          <div class="chip-row mt-xs">
            <cp-chip :tone="$cp.toneClass(job.status)">{{ $cp.statusLabel(job.status) }}</cp-chip>
            <cp-chip :tone="$cp.toneClass(job.phase)">{{ $cp.phaseLabel(job.phase) || job.phase }}</cp-chip>
            <cp-chip>{{ job.priority || '-' }}</cp-chip>
            <cp-chip>{{ job.agent || 'auto' }}</cp-chip>
            <cp-chip>{{ job.planner || '-' }}</cp-chip>
          </div>
          <div class="detail-actions">
            <button v-if="canCancel" class="btn btn-danger btn-sm" :disabled="cancelPending" @click="cancelJob">
              {{ cancelPending ? '停止中' : '停止' }}
            </button>
            <button v-if="canRetry" class="btn btn-outline btn-sm" :disabled="retryPending" @click="retryJob">
              {{ retryPending ? '重试中' : '重试' }}
            </button>
          </div>
        </div>
        <div class="card-body task-detail task-detail-body">
          <div class="task-kv-grid job-kv-grid">
            <div class="task-kv-item">
              <span class="task-kv-key">项目</span>
              <span class="task-kv-value">{{ job.project || s.nav.project || '-' }}</span>
            </div>
            <div class="task-kv-item">
              <span class="task-kv-key">创建时间</span>
              <span class="task-kv-value">{{ $cp.fmtTime(job.created_at) }}</span>
            </div>
            <div class="task-kv-item">
              <span class="task-kv-key">更新时间</span>
              <span class="task-kv-value">{{ $cp.fmtTime(job.updated_at) }}</span>
            </div>
            <div class="task-kv-item">
              <span class="task-kv-key">结束时间</span>
              <span class="task-kv-value">{{ $cp.fmtTime(job.finished_at) }}</span>
            </div>
            <div class="task-kv-item span-2">
              <span class="task-kv-key">关联任务</span>
              <span class="task-kv-value task-kv-value-wrap">
                <template v-if="job.task_ids && job.task_ids.length">
                  <button v-for="id in job.task_ids" :key="id" class="chip primary" @click="cp.selectTask(s.nav.project, id)">#{{ id }}</button>
                </template>
                <span v-else class="muted">无</span>
              </span>
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
