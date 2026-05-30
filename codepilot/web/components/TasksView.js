/* Workflow task board for the current project. */
/* global Vue, CP */
CP.Components.TasksView = Vue.defineComponent({
  name: 'CpTasksView',
  inject: ['cp'],
  data() {
    return {
      inlineConfirm: { taskId: null, action: '' },
    };
  },
  computed: {
    s() { return this.cp.state; },
    board() {
      return this.s.taskBoard || { columns: [], counts: {}, total: 0, sort: { column_order: [] } };
    },
    boardColumns() {
      return Array.isArray(this.board.columns) ? this.board.columns : [];
    },
    boardTotal() {
      return Number(this.board.total || 0);
    },
  },
  watch: {
    'cp.state.nav.project'() {
      this.clearInlineConfirm();
    },
  },
  methods: {
    pick(task) {
      if (!task || !task.id) return;
      this.cp.selectTask(task.project || this.s.nav.project, task.id);
    },
    actionLabel(act) {
      return {
        promote: '插队',
        retry: '重试',
        stop: '停止',
        cancel: '取消',
        archive: '归档',
        delete: '删除',
      }[act] || act;
    },
    actionToneClass(act) {
      if (act === 'retry' || act === 'cancel') return 'btn-warning';
      if (act === 'stop' || act === 'delete') return 'btn-danger';
      return 'btn-outline';
    },
    cardActions(task) {
      const actions = (task && task.actions) || {};
      return ['promote', 'retry', 'stop', 'cancel', 'archive', 'delete']
        .filter(act => !!actions[act]);
    },
    needsInlineConfirm(act) {
      return ['cancel', 'archive', 'delete', 'stop'].includes(act);
    },
    isInlineConfirm(task) {
      return !!task && this.inlineConfirm.taskId === task.id;
    },
    clearInlineConfirm() {
      this.inlineConfirm = { taskId: null, action: '' };
    },
    inlineConfirmMessage(task) {
      if (!task || !task.id) return '';
      const label = this.actionLabel(this.inlineConfirm.action);
      return `${label}任务 #${task.id}？`;
    },
    confirmInlineAction(task) {
      if (!task || !task.id || !this.inlineConfirm.action) return;
      const act = this.inlineConfirm.action;
      this.clearInlineConfirm();
      this.cp.taskAction(task.id, act);
    },
    action(task, act, ev) {
      if (ev) ev.stopPropagation();
      if (!task || !task.id || !act) return;
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
    openLogs(task, ev) {
      if (ev) ev.stopPropagation();
      this.pick(task);
    },
  },
  template: `
    <div class="view workflow-board-view">
      <div class="view-toolbar">
        <div>
          <h2 class="view-title">编程工作流任务板</h2>
          <div class="muted tiny">Backlog / Ready / Running / Review / Blocked / Done · 共 {{ boardTotal }} 个</div>
        </div>
        <button class="btn btn-outline btn-sm" @click="cp.loadDashboard()" :disabled="s.loading">
          <span v-if="s.loading" class="spinner"></span>
          刷新
        </button>
      </div>
      <div class="workflow-board">
        <section v-for="column in boardColumns"
                 :key="column.id"
                 class="workflow-column"
                 :class="'tone-' + (column.tone || 'neutral')">
          <div class="workflow-column-head">
            <div>
              <h3>{{ column.title }}</h3>
              <p>{{ column.description }}</p>
            </div>
            <span class="workflow-column-count">{{ column.count }}</span>
          </div>
          <div v-if="!column.tasks.length" class="workflow-column-empty">
            暂无任务
          </div>
          <article v-for="task in column.tasks"
                   :key="task.id"
                   class="workflow-task-card"
                   @click="pick(task)">
            <div class="workflow-card-title">#{{ task.id }} {{ task.title }}</div>
            <div class="chip-row">
              <cp-chip :tone="$cp.toneClass(task.status)">{{ $cp.statusLabel(task.status) }}</cp-chip>
              <cp-chip>{{ task.priority }}</cp-chip>
              <cp-chip>{{ task.agent || '-' }}</cp-chip>
              <cp-chip v-if="task.source === 'auto-inspect'" tone="warning">巡检</cp-chip>
            </div>
            <div class="workflow-card-meta">
              <span>{{ task.execution_status || task.status || '-' }}</span>
              <span>阶段 {{ task.phase || '-' }}</span>
              <span>更新 {{ $cp.fmtTime(task.updated_at || task.completed_at || task.started_at || task.created_at) }}</span>
            </div>
            <div class="workflow-card-blocked" v-if="task.blocked_reason">
              {{ task.blocked_reason }}
            </div>
            <div class="workflow-card-preview"
                 :class="{ 'preview-warning': task.skip_reason || task.blocked_reason, 'preview-danger': task.error_message }">
              {{ task.runtime || task.latest || task.error_message || task.delivery_record || '暂无详细信息' }}
            </div>
            <div class="workflow-card-actions" @click.stop>
              <button class="btn btn-outline btn-sm" @click.stop="openLogs(task, $event)">
                日志
              </button>
              <button v-for="act in cardActions(task)"
                      :key="act"
                      class="btn btn-sm"
                      :class="actionToneClass(act)"
                      :disabled="cp.isTaskPending(task.id)"
                      @click.stop="action(task, act, $event)">
                <span v-if="cp.taskPendingAction(task.id) === act" class="spinner"></span>
                {{ actionLabel(act) }}
              </button>
              <div v-if="isInlineConfirm(task)" class="inline-quick-confirm workflow-inline-confirm" @click.stop>
                <div class="inline-quick-confirm-text">{{ inlineConfirmMessage(task) }}</div>
                <div class="inline-quick-confirm-actions">
                  <button class="btn btn-primary btn-sm"
                          :disabled="cp.isTaskPending(task.id)"
                          @click.stop="confirmInlineAction(task)">
                    确认
                  </button>
                  <button class="btn btn-outline btn-sm"
                          :disabled="cp.isTaskPending(task.id)"
                          @click.stop="clearInlineConfirm()">
                    取消
                  </button>
                </div>
              </div>
            </div>
          </article>
        </section>
      </div>
    </div>
  `,
});
