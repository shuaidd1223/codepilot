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
      selectedTaskIds: [],
      batchConfirm: { action: '' },
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
    selectedTasks() {
      if (!Array.isArray(this.tasks) || !this.selectedTaskIds.length) return [];
      const selected = new Set(this.selectedTaskIds);
      return this.tasks.filter(t => t && selected.has(t.id));
    },
    selectedCount() {
      return this.selectedTasks.length;
    },
    selectedAll() {
      return !!this.tasks.length && this.selectedCount === this.tasks.length;
    },
    showBatchBar() {
      return !!this.tasks.length;
    },
  },
  watch: {
    'cp.state.nav.project'() {
      this.visibleCount = this.pageSize;
      this.clearInlineConfirm();
      this.clearBatchConfirm();
      this.selectedTaskIds = [];
    },
    tasks(nextTasks) {
      if (!Array.isArray(nextTasks)) {
        this.visibleCount = this.pageSize;
        this.clearInlineConfirm();
        this.clearBatchConfirm();
        this.selectedTaskIds = [];
        return;
      }
      if (nextTasks.length <= this.pageSize) {
        this.visibleCount = this.pageSize;
      } else if (this.visibleCount > nextTasks.length) {
        this.visibleCount = Math.max(this.pageSize, nextTasks.length);
      }
      const validIds = new Set(nextTasks.map(t => (t && t.id)).filter(Boolean));
      this.selectedTaskIds = this.selectedTaskIds.filter(id => validIds.has(id));
      if (!this.selectedTaskIds.length) this.clearBatchConfirm();
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
    clearBatchConfirm() {
      this.batchConfirm = { action: '' };
    },
    isSelected(task) {
      return !!task && this.selectedTaskIds.includes(task.id);
    },
    toggleSelected(task, ev) {
      if (ev) ev.stopPropagation();
      if (!task || !task.id) return;
      if (this.isSelected(task)) {
        this.selectedTaskIds = this.selectedTaskIds.filter(id => id !== task.id);
      } else {
        this.selectedTaskIds = [...this.selectedTaskIds, task.id];
      }
      if (!this.selectedTaskIds.length) this.clearBatchConfirm();
    },
    toggleSelectAll(ev) {
      if (ev) ev.stopPropagation();
      if (!this.tasks.length) return;
      if (this.selectedAll) {
        this.selectedTaskIds = [];
        this.clearBatchConfirm();
        return;
      }
      this.selectedTaskIds = this.tasks.map(t => t.id);
    },
    clearSelection(ev) {
      if (ev) ev.stopPropagation();
      this.selectedTaskIds = [];
      this.clearBatchConfirm();
    },
    selectedActionIds(act) {
      if (!this.selectedCount) return [];
      const selected = new Set(this.selectedTaskIds);
      const ids = [];
      for (const task of (this.tasks || [])) {
        if (!task || !selected.has(task.id)) continue;
        if (task.actions && task.actions[act]) ids.push(task.id);
      }
      return ids;
    },
    selectedBlockedCount(act) {
      return Math.max(this.selectedCount - this.selectedActionIds(act).length, 0);
    },
    canBatchAction(act) {
      return this.selectedActionIds(act).length > 0;
    },
    batchConfirmMessage() {
      const act = this.batchConfirm.action;
      const count = this.selectedActionIds(act).length;
      if (!act || count <= 0) return '';
      const blocked = this.selectedBlockedCount(act);
      const verb = act === 'cancel' ? '取消' : (act === 'archive' ? '归档' : '删除');
      return blocked > 0
        ? `批量${verb} ${count} 个任务？（${blocked} 个不满足条件会跳过）`
        : `批量${verb} ${count} 个任务？`;
    },
    requestBatchAction(act, ev) {
      if (ev) ev.stopPropagation();
      if (!this.canBatchAction(act)) return;
      if (this.batchConfirm.action === act) {
        this.clearBatchConfirm();
      } else {
        this.batchConfirm = { action: act };
      }
    },
    async confirmBatchAction(ev) {
      if (ev) ev.stopPropagation();
      const act = this.batchConfirm.action;
      const ids = this.selectedActionIds(act);
      this.clearBatchConfirm();
      if (!act || !ids.length) return;
      await this.cp.taskBatchAction(ids, act);
      const idSet = new Set(ids);
      this.selectedTaskIds = this.selectedTaskIds.filter(id => !idSet.has(id));
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
      <div v-if="showBatchBar" class="task-batch-bar">
        <div class="task-batch-left">
          <label class="task-batch-check" @click.stop>
            <input type="checkbox" :checked="selectedAll" @change="toggleSelectAll($event)">
            <span>全选</span>
          </label>
          <button class="btn btn-outline btn-sm" @click.stop="clearSelection($event)" :disabled="!selectedCount">
            清空
          </button>
          <span class="muted tiny" v-if="selectedCount">已选 {{ selectedCount }}</span>
        </div>
        <div class="task-batch-actions">
          <button class="btn btn-warning btn-sm"
                  :disabled="!canBatchAction('cancel')"
                  @click.stop="requestBatchAction('cancel', $event)">
            批量取消
          </button>
          <button class="btn btn-outline btn-sm"
                  :disabled="!canBatchAction('archive')"
                  @click.stop="requestBatchAction('archive', $event)">
            批量归档
          </button>
          <button class="btn btn-danger btn-sm"
                  :disabled="!canBatchAction('delete')"
                  @click.stop="requestBatchAction('delete', $event)">
            批量删除
          </button>
        </div>
        <div v-if="batchConfirm.action" class="inline-batch-confirm" @click.stop>
          <div class="inline-batch-confirm-text">{{ batchConfirmMessage() }}</div>
          <div class="inline-batch-confirm-actions">
            <button class="btn btn-primary btn-sm" @click.stop="confirmBatchAction($event)">确认</button>
            <button class="btn btn-outline btn-sm" @click.stop="clearBatchConfirm()">取消</button>
          </div>
        </div>
      </div>
      <div v-if="!tasks.length" class="empty pad">{{ empty }}</div>
      <div v-else class="task-list">
        <article v-for="x in visibleTasks" :key="x.id" class="task-item" @click="pick(x.id)">
          <label class="task-item-select" @click.stop>
            <input type="checkbox" :checked="isSelected(x)" @change="toggleSelected(x, $event)">
          </label>
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
            <div class="task-phase-progress task-phase-progress-detail" :class="'tone-' + $cp.taskPhaseProgress(x).tone">
              <div class="task-phase-progress-head">
                <div class="task-phase-current">
                  <span class="phase-pulse" v-if="x.status === 'in_progress'"></span>
                  {{ $cp.taskPhaseProgress(x).currentLabel }}
                </div>
                <div class="task-phase-next">{{ $cp.taskPhaseProgress(x).nextLabel }}</div>
              </div>
              <div class="task-phase-steps">
                <span v-for="step in $cp.taskPhaseProgress(x).steps"
                      :key="step.key"
                      class="task-phase-step"
                      :class="'state-' + step.state">
                  <span class="task-phase-dot"></span>
                  <span>{{ step.label }}</span>
                </span>
              </div>
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
            全部收起
          </button>
        </div>
      </div>
    </section>
  `,
});

