/* Project home OpenCode session workspace with project context. */
/* global Vue, CP */
CP.Components.ProjectView = Vue.defineComponent({
  name: 'CpProjectView',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    currentProject() { return this.cp.currentProject; },
    projectSessions() { return this.cp.projectSessions || []; },
    runningTasks() { return this.s.tasks.filter(t => t.status === 'in_progress'); },
    taskService() { return (this.currentProject && this.currentProject.services && this.currentProject.services.tasks) || {}; },
    inspectService() { return (this.currentProject && this.currentProject.services && this.currentProject.services.inspect) || {}; },
    workflowInspect() { return (this.currentProject && this.currentProject.workflow && this.currentProject.workflow.inspect) || {}; },
    workflowActions() { return (this.currentProject && this.currentProject.workflow && this.currentProject.workflow.next_actions) || []; },
    reportOnlyItems() { return this.workflowInspect.report_only || []; },
    createdPreviewItems() { return this.workflowInspect.created_preview || []; },
    qualitySummary() { return this.workflowInspect.quality_summary || {}; },
  },
  methods: {
    servicePending(service, action) {
      return this.s.servicePending === service + ':' + action;
    },
    isSessionActive(session) {
      return !!session && Number(session.id) === Number(this.s.activeProjectSessionId || 0);
    },
    sessionTitle(session) {
      const title = (session && session.title) || '新会话';
      return `#${session.id} ${title}`;
    },
    workflowPending(action) {
      return this.s.workflowPending === action;
    },
    workflowActionLabel(action) {
      return (action && action.label) || (action && action.id) || '执行';
    },
  },
  template: `
    <div class="view project-console-view opencode-workspace-view">
      <div class="view-toolbar">
        <div>
          <h2 class="view-title">{{ currentProject ? currentProject.name : '项目' }}</h2>
          <div class="muted tiny">{{ currentProject ? currentProject.path : '选择项目后开始工作' }}</div>
        </div>
        <div class="toolbar-actions">
          <cp-chip :tone="taskService.running || inspectService.running ? 'success' : 'neutral'">
            {{ taskService.running || inspectService.running ? '服务运行中' : '服务未运行' }}
          </cp-chip>
          <button class="btn btn-outline btn-sm" @click="cp.loadDashboard()" :disabled="s.loading">
            <span v-if="s.loading" class="spinner"></span>
            刷新
          </button>
          <button class="btn btn-outline btn-sm" @click="cp.openSessionPage(s.nav.project, s.activeProjectSessionId)" :disabled="!s.activeProjectSessionId">
            会话详情
          </button>
        </div>
      </div>

      <div class="project-workbench-grid">
        <aside class="project-session-list ops-panel">
          <div class="session-list-head">
            <div>
              <h3>会话</h3>
              <p class="muted">当前项目的 OpenCode 对话</p>
            </div>
            <button class="btn btn-primary btn-sm" @click="cp.newSession(s.nav.project)" :disabled="s.newSessionLoading">
              <span v-if="s.newSessionLoading" class="spinner"></span>
              新建
            </button>
          </div>
          <div v-if="!projectSessions.length" class="session-list-empty">
            暂无会话，直接在中间输入即可创建
          </div>
          <button
            v-for="se in projectSessions"
            :key="se.id"
            class="session-list-item"
            :class="{active: isSessionActive(se)}"
            @click="cp.selectEmbeddedSession(se.project, se.id)">
            <span class="session-list-title">{{ sessionTitle(se) }}</span>
            <span class="session-list-meta">
              <span>{{ se.message_count || 0 }} 条</span>
              <span>{{ $cp.fmtTime(se.updated_at) }}</span>
            </span>
          </button>
        </aside>

        <section class="project-session-workbench opencode-session-card ops-panel">
          <div class="opencode-workbench-head">
            <div>
              <h3>OpenCode</h3>
              <p class="muted">会话就是和 OpenCode 的交互，输入后立即进入流式执行</p>
            </div>
          </div>
          <cp-session-chat-panel embedded></cp-session-chat-panel>
        </section>

        <aside class="project-context-rail">
          <section class="project-service-panel ops-panel">
            <div class="card-head">
              <h3>项目服务</h3>
              <p class="muted">执行器与巡检进程</p>
            </div>
            <div class="service-row">
              <div class="service-main">
                <div class="service-title">任务执行</div>
                <div class="muted tiny">
                  {{ taskService.running ? (taskService.stopping ? ('停止中，PID ' + taskService.pid) : ('PID ' + taskService.pid)) : '未运行' }}
                </div>
              </div>
              <div class="row gap-xs">
                <button class="btn btn-primary btn-sm" @click="cp.projectService('tasks', 'start')" :disabled="taskService.running || !!s.servicePending">
                  <span v-if="servicePending('tasks', 'start')" class="spinner"></span>
                  启动
                </button>
                <button class="btn btn-danger-outline btn-sm" @click="cp.projectService('tasks', 'stop')" :disabled="!taskService.running || taskService.stopping || !!s.servicePending">
                  <span v-if="servicePending('tasks', 'stop')" class="spinner"></span>
                  停止
                </button>
              </div>
            </div>
            <div class="service-row">
              <div class="service-main">
                <div class="service-title">项目巡检</div>
                <div class="muted tiny">{{ inspectService.running ? ('PID ' + inspectService.pid) : '未运行' }}</div>
              </div>
              <div class="row gap-xs">
                <button class="btn btn-primary btn-sm" @click="cp.projectService('inspect', 'start')" :disabled="inspectService.running || !!s.servicePending">
                  <span v-if="servicePending('inspect', 'start')" class="spinner"></span>
                  启动
                </button>
                <button class="btn btn-danger-outline btn-sm" @click="cp.projectService('inspect', 'stop')" :disabled="!inspectService.running || !!s.servicePending">
                  <span v-if="servicePending('inspect', 'stop')" class="spinner"></span>
                  停止
                </button>
              </div>
            </div>
          </section>

          <cp-metrics-panel class="project-metrics-panel"></cp-metrics-panel>

          <section class="project-inspect-workflow-panel ops-panel">
            <div class="card-head">
              <h3>巡检工作流</h3>
              <p class="muted">{{ workflowInspect.context_path ? '最近一次巡检建议' : '尚未生成巡检上下文' }}</p>
            </div>
            <div class="row gap-xs wrap">
              <button class="btn btn-primary btn-sm" @click="cp.runInspectWorkflow(s.nav.project)" :disabled="!!s.workflowPending">
                <span v-if="workflowPending('inspect')" class="spinner"></span>
                运行巡检
              </button>
              <button
                v-for="action in workflowActions"
                :key="action.id"
                class="btn btn-outline btn-sm"
                @click="cp.workflowAction(action.id, s.nav.project)"
                :disabled="!!s.workflowPending">
                <span v-if="workflowPending(action.id)" class="spinner"></span>
                {{ workflowActionLabel(action) }}
              </button>
            </div>
            <div class="project-status-grid inspect-summary-grid">
              <div class="project-status-tile">
                <div class="project-status-label">可建任务</div>
                <div class="project-status-value">{{ qualitySummary.created_count || createdPreviewItems.length || 0 }}</div>
              </div>
              <div class="project-status-tile">
                <div class="project-status-label">仅报告</div>
                <div class="project-status-value">{{ qualitySummary.report_only_count || reportOnlyItems.length || 0 }}</div>
              </div>
            </div>
            <div v-if="reportOnlyItems.length" class="task-list compact-task-list inspect-report-list">
              <article v-for="item in reportOnlyItems.slice(0, 4)" :key="item.candidate_id" class="task-item">
                <div class="task-item-body">
                  <div class="task-item-title">{{ item.title }}</div>
                  <div class="chip-row">
                    <cp-chip>{{ item.priority || 'P3' }}</cp-chip>
                    <cp-chip>{{ item.reason || 'report_only' }}</cp-chip>
                  </div>
                  <div class="task-item-preview">{{ (item.files || []).join(', ') || item.goal }}</div>
                </div>
              </article>
            </div>
          </section>

          <section class="project-running-panel ops-panel">
            <div class="card-head">
              <h3>当前运行中</h3>
              <p class="muted">{{ runningTasks.length ? '点击任务跳到详情' : '暂无运行任务' }}</p>
            </div>
            <div v-if="runningTasks.length" class="task-list compact-task-list">
              <article v-for="x in runningTasks" :key="x.id" class="task-item" @click="cp.selectTask(s.nav.project, x.id)">
                <div class="task-item-body">
                  <div class="task-item-title">#{{ x.id }} {{ x.title }}</div>
                  <div class="chip-row">
                    <cp-chip :tone="$cp.toneClass(x.status)">{{ $cp.statusLabel(x.status) }}</cp-chip>
                    <cp-chip>{{ x.priority }}</cp-chip>
                    <cp-chip>{{ x.agent || '-' }}</cp-chip>
                  </div>
                  <div class="task-item-meta">
                    <span>阶段: {{ x.phase || '-' }}</span>
                    <span v-if="x.created_at">创建: {{ $cp.fmtTime(x.created_at) }}</span>
                  </div>
                  <div class="task-phase-progress" :class="'tone-' + $cp.taskPhaseProgress(x).tone">
                    <div class="task-phase-progress-head">
                      <div class="task-phase-current">
                        <span class="phase-pulse"></span>
                        {{ $cp.taskPhaseProgress(x).currentLabel }}
                      </div>
                      <div class="task-phase-next">{{ $cp.taskPhaseProgress(x).nextLabel }}</div>
                    </div>
                    <div class="task-phase-track">
                      <span class="task-phase-fill" :style="{ width: $cp.taskPhaseProgress(x).percent + '%' }"></span>
                    </div>
                  </div>
                  <div class="task-item-preview">{{ x.runtime || x.latest || '暂无详细信息' }}</div>
                </div>
              </article>
            </div>
          </section>
        </aside>
      </div>
    </div>
  `,
});
