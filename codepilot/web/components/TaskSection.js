/* Reusable task list section. */
/* global Vue, CP */
CP.Components.TaskSection = Vue.defineComponent({
  name: 'CpTaskSection',
  inject: ['cp'],
  props: {
    title: { type: String, required: true },
    tasks: { type: Array, required: true },
    empty: { type: String, default: '暂无任务' },
  },
  data() {
    return {
      pageSize: 10,
      visibleCount: 10,
      inlineConfirm: { taskId: null, action: '' },
    };
  },
  computed: {
    visibleTasks() {
      return this.tasks.slice(0, this.visibleCount);
    },
    hasMore() {
      return this.tasks.length > this.visibleCount;
    },
    canCollapse() {
      return this.tasks.length > this.pageSize && this.visibleCount > this.pageSize;
    },
    remainingCount() {
      return Math.max(this.tasks.length - this.visibleCount, 0);
    },
    nextChunkCount() {
      return Math.min(this.pageSize, this.remainingCount);
    },
  },
  watch: {
    'cp.state.nav.project'() {
      this.visibleCount = this.pageSize;
      this.clearInlineConfirm();
    },
    tasks(nextTasks) {
      if (!Array.isArray(nextTasks)) {
        this.visibleCount = this.pageSize;
        this.clearInlineConfirm();
        return;
      }
      if (nextTasks.length <= this.pageSize) {
        this.visibleCount = this.pageSize;
      } else if (this.visibleCount > nextTasks.length) {
        this.visibleCount = Math.max(this.pageSize, nextTasks.length);
      }
      if (this.inlineConfirm.taskId != null && !nextTasks.some(t => t && t.id === this.inlineConfirm.taskId)) {
        this.clearInlineConfirm();
      }
    },
  },
  methods: {
    pick(id) { this.cp.selectTask(this.cp.state.nav.project, id); },
    needsInlineConfirm(act) {
      return ['cancel', 'archive', 'delete'].includes(act);
    },
    isInlineConfirm(task) {
      return !!task && this.inlineConfirm.taskId === task.id;
    },
    clearInlineConfirm() {
      this.inlineConfirm = { taskId: null, action: '' };
    },
    inlineConfirmMessage(task) {
      if (!task || !task.id) return '';
      if (this.inlineConfirm.action === 'cancel') return `取消任务 #${task.id}？`;
      if (this.inlineConfirm.action === 'archive') return `归档任务 #${task.id}？`;
      if (this.inlineConfirm.action === 'delete') return `删除任务 #${task.id}？`;
      return '';
    },
    confirmInlineAction(task) {
      if (!task || !task.id || !this.inlineConfirm.action) return;
      const act = this.inlineConfirm.action;
      this.clearInlineConfirm();
      this.cp.taskAction(task.id, act);
    },
    action(task, act, ev) {
      if (ev) ev.stopPropagation();
      if (!task || !task.id) return;
      if (!this.needsInlineConfirm(act)) {
        this.clearInlineConfirm();
        this.cp.taskAction(task.id, act);
        return;
      }
      if (this.inlineConfirm.taskId === task.id && this.inlineConfirm.action === act) {
        this.clearInlineConfirm();
      } else {
        this.inlineConfirm = { taskId: task.id, action: act };
      }
    },
    loadMore() {
      this.visibleCount += this.pageSize;
    },
    collapseList() {
      this.visibleCount = this.pageSize;
    },
  },
  template: `
    <section class="card">
      <div class="task-section-head">
        <h3>{{ title }}</h3>
        <span class="muted tiny">{{ tasks.length }}</span>
      </div>
      <div v-if="!tasks.length" class="empty pad">{{ empty }}</div>
      <div v-else class="task-list">
        <article v-for="x in visibleTasks" :key="x.id" class="task-item" @click="pick(x.id)">
          <div class="task-item-body">
            <div class="task-item-title">#{{ x.id }} {{ x.title }}</div>
            <div class="chip-row">
              <cp-chip :tone="$cp.toneClass(x.status)">{{ $cp.statusLabel(x.status) }}</cp-chip>
              <cp-chip>{{ x.priority }}</cp-chip>
              <cp-chip>{{ x.agent || '-' }}</cp-chip>
              <cp-chip>重试 {{ x.retry_count || 0 }}/{{ x.max_retries || 0 }}</cp-chip>
              <cp-chip v-if="x.source === 'auto-inspect'" tone="warning">巡检</cp-chip>
              <cp-chip v-if="x.skip_reason" tone="warning" tiny>预检跳过</cp-chip>
            </div>
            <div class="task-item-meta">
              <span>阶段: {{ x.phase || '-' }}</span>
              <span v-if="x.created_at">创建: {{ $cp.fmtTime(x.created_at) }}</span>
              <span v-if="x.started_at">开始: {{ $cp.fmtTime(x.started_at) }}</span>
              <span v-if="x.completed_at">完成: {{ $cp.fmtTime(x.completed_at) }}</span>
            </div>
            <div class="task-item-preview"
                 :class="{ 'preview-warning': x.skip_reason, 'preview-danger': !x.skip_reason && x.error_message }">
              {{ x.runtime || x.skip_reason || x.error_message || x.latest || '暂无详细信息' }}
            </div>
          </div>
          <div class="task-item-actions" @click.stop>
            <button v-if="x.actions.promote" class="btn btn-outline btn-sm" @click="action(x, 'promote', $event)"
                    :disabled="cp.isTaskPending(x.id)">
              <span v-if="cp.taskPendingAction(x.id) === 'promote'" class="spinner"></span>
              插队
            </button>
            <button v-if="x.actions.retry" class="btn btn-warning btn-sm" @click="action(x, 'retry', $event)"
                    :disabled="cp.isTaskPending(x.id)">
              <span v-if="cp.taskPendingAction(x.id) === 'retry'" class="spinner"></span>
              重试
            </button>
            <button v-if="x.actions.cancel" class="btn btn-warning btn-sm" @click="action(x, 'cancel', $event)"
                    :disabled="cp.isTaskPending(x.id)">
              <span v-if="cp.taskPendingAction(x.id) === 'cancel'" class="spinner"></span>
              取消
            </button>
            <button v-if="x.actions.archive" class="btn btn-outline btn-sm" @click="action(x, 'archive', $event)"
                    :disabled="cp.isTaskPending(x.id)">
              <span v-if="cp.taskPendingAction(x.id) === 'archive'" class="spinner"></span>
              归档
            </button>
            <button v-if="x.actions.delete" class="btn btn-danger btn-sm" @click="action(x, 'delete', $event)"
                    :disabled="cp.isTaskPending(x.id)">
              <span v-if="cp.taskPendingAction(x.id) === 'delete'" class="spinner"></span>
              删除
            </button>
            <div v-if="isInlineConfirm(x)" class="inline-quick-confirm" @click.stop>
              <div class="inline-quick-confirm-text">{{ inlineConfirmMessage(x) }}</div>
              <div class="inline-quick-confirm-actions">
                <button class="btn btn-primary btn-sm"
                        :disabled="cp.isTaskPending(x.id)"
                        @click.stop="confirmInlineAction(x)">
                  确认
                </button>
                <button class="btn btn-outline btn-sm"
                        :disabled="cp.isTaskPending(x.id)"
                        @click.stop="clearInlineConfirm()">
                  取消
                </button>
              </div>
            </div>
          </div>
        </article>
        <div v-if="hasMore || canCollapse" class="list-load-more">
          <button v-if="hasMore" class="btn btn-outline btn-sm" @click.stop="loadMore">
            再展开 {{ nextChunkCount }} 条（剩余 {{ remainingCount }}）
          </button>
          <button v-if="canCollapse" class="btn btn-outline btn-sm" @click.stop="collapseList">
            收起到 {{ pageSize }} 条
          </button>
        </div>
      </div>
    </section>
  `,
});
