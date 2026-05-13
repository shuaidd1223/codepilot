/* List all jobs (requirements) for current project. */
/* global Vue, CP */
CP.Components.JobsView = Vue.defineComponent({
  name: 'CpJobsView',
  inject: ['cp'],
  data() {
    return {
      pageSize: 10,
      visibleCount: 10,
    };
  },
  computed: {
    s() { return this.cp.state; },
    jobs() { return this.s.jobs || []; },
    visibleJobs() {
      return this.jobs.slice(0, this.visibleCount);
    },
    hasMore() {
      return this.jobs.length > this.visibleCount;
    },
    canCollapse() {
      return this.jobs.length > this.pageSize && this.visibleCount > this.pageSize;
    },
    remainingCount() {
      return Math.max(this.jobs.length - this.visibleCount, 0);
    },
    nextChunkCount() {
      return Math.min(this.pageSize, this.remainingCount);
    },
  },
  watch: {
    'cp.state.nav.project'() {
      this.visibleCount = this.pageSize;
    },
    jobs(nextJobs) {
      if (!Array.isArray(nextJobs)) {
        this.visibleCount = this.pageSize;
        return;
      }
      if (nextJobs.length <= this.pageSize) {
        this.visibleCount = this.pageSize;
        return;
      }
      if (this.visibleCount > nextJobs.length) {
        this.visibleCount = Math.max(this.pageSize, nextJobs.length);
      }
    },
  },
  methods: {
    canCancel(job) {
      return job && this.$cp.isJobActive(job) && !job.cancel_requested;
    },
    canRetry(job) {
      return job && !this.$cp.isJobActive(job);
    },
    jobAction(job, action) {
      this.cp.jobAction(job, action);
    },
    jobActionPending(job, action) {
      return !!(job && this.cp.isActionPending(`${this.cp.ACTION_KEYS.JOB_ACTION}:${job.id}:${action}`));
    },
    loadMore() {
      this.visibleCount += this.pageSize;
    },
    collapseList() {
      this.visibleCount = this.pageSize;
    },
  },
  template: `
    <div class="view">
      <div class="view-toolbar">
        <h2 class="view-title">需求</h2>
        <div class="muted tiny">由用户自然语言拆分成的需求（保留最近 200 条历史记录）</div>
      </div>
      <div v-if="!jobs.length" class="empty pad">还没有需求，从项目概览提交一条试试</div>
      <div v-else class="cards-grid">
        <button v-for="j in visibleJobs" :key="j.id" class="item-card" @click="cp.selectJob(s.nav.project, j.id)">
          <div class="row between">
            <strong class="truncate">
              <span v-if="$cp.isJobActive(j)" class="spinner"></span>
              #{{ j.id }} {{ j.title }}
            </strong>
            <cp-chip :tone="$cp.toneClass(j.status)">{{ $cp.phaseLabel(j.phase) || j.status }}</cp-chip>
          </div>
          <div class="muted tiny">创建: {{ $cp.fmtTime(j.created_at) }} · planner: {{ j.planner || '-' }} · agent: {{ j.agent || 'auto' }}</div>
          <div v-if="j.summary || j.error" class="tiny">{{ j.summary || j.error }}</div>
          <div class="item-actions">
            <button v-if="canCancel(j)" class="btn btn-danger btn-sm" :disabled="jobActionPending(j, 'cancel')" @click.stop="jobAction(j, 'cancel')">
              {{ jobActionPending(j, 'cancel') ? '停止中' : '停止' }}
            </button>
            <button v-if="canRetry(j)" class="btn btn-outline btn-sm" :disabled="jobActionPending(j, 'retry')" @click.stop="jobAction(j, 'retry')">
              {{ jobActionPending(j, 'retry') ? '重试中' : '重试' }}
            </button>
          </div>
        </button>
      </div>
      <div v-if="hasMore || canCollapse" class="list-load-more">
        <button v-if="hasMore" class="btn btn-outline btn-sm" @click="loadMore">
          再展开 {{ nextChunkCount }} 条（剩余 {{ remainingCount }}）
        </button>
        <button v-if="canCollapse" class="btn btn-outline btn-sm" @click="collapseList">
          全部收起
        </button>
      </div>
    </div>
  `,
});

