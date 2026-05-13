/* Project-first tree sidebar: projects → (tasks, jobs). */
/* global Vue, CP */
CP.Components.Sidebar = Vue.defineComponent({
  name: 'CpSidebar',
  inject: ['cp'],
  data() {
    return {
      pageSize: 10,
      visibleLeafCount: {},
      leafConfirm: { taskId: null, action: '' },
    };
  },
  computed: {
    s() { return this.cp.state; },
  },
  methods: {
    isExpanded(key, def) { return this.cp.isExpanded(key, def); },
    toggleProject(name, ev) {
      ev.stopPropagation();
      this.cp.toggleProject(name);
    },
    toggleCategory(project, cat, ev) {
      ev.stopPropagation();
      this.cp.toggleCategory(project, cat);
    },
    isActive(match) {
      const n = this.s.nav;
      return Object.keys(match).every(k => n[k] === match[k]);
    },
    projectTasks(projName) {
      /* Read from the per-project map so this project's tasks never show
       * some other project's content during a nav switch. The map is
       * populated by loadDashboard whenever a project is selected; other
       * projects simply stay at [] until their dashboard is fetched. */
      return (this.s.tasksByProject && this.s.tasksByProject[projName]) || [];
    },
    projectJobs(projName) {
      return (this.s.jobsByProject && this.s.jobsByProject[projName]) || [];
    },
    submitProject() {
      this.cp.submitProject();
    },
    deleteProject(project, ev) {
      ev.stopPropagation();
      this.cp.deleteProject(project.name);
    },
    /* Count helpers: look at project summary first so numbers stay correct
     * across projects even when we only loaded the current project's list. */
    taskCount(p) {
      if (p && p.stats && typeof p.stats.total === 'number') return p.stats.total;
      return this.projectTasks(p.name).length;
    },
    jobCount(p) {
      if (p && typeof p.job_count === 'number') return p.job_count;
      return this.projectJobs(p.name).length;
    },
    leafKey(project, category) {
      return `${project}/${category}`;
    },
    leafLimit(project, category) {
      const key = this.leafKey(project, category);
      return this.visibleLeafCount[key] || this.pageSize;
    },
    visibleLeafItems(items, project, category) {
      return (items || []).slice(0, this.leafLimit(project, category));
    },
    leafRemaining(items, project, category) {
      return Math.max((items || []).length - this.leafLimit(project, category), 0);
    },
    leafHasMore(items, project, category) {
      return this.leafRemaining(items, project, category) > 0;
    },
    leafCanCollapse(items, project, category) {
      return (items || []).length > this.pageSize && this.leafLimit(project, category) > this.pageSize;
    },
    leafNextChunk(items, project, category) {
      return Math.min(this.pageSize, this.leafRemaining(items, project, category));
    },
    expandLeaf(project, category) {
      const key = this.leafKey(project, category);
      this.visibleLeafCount[key] = this.leafLimit(project, category) + this.pageSize;
    },
    collapseLeaf(project, category) {
      const key = this.leafKey(project, category);
      this.visibleLeafCount[key] = this.pageSize;
    },
    taskActionNeedsConfirm(act) {
      return ['cancel', 'archive', 'delete'].includes(act);
    },
    isTaskQuickConfirm(task) {
      return !!task && this.leafConfirm.taskId === task.id;
    },
    clearTaskQuickConfirm() {
      this.leafConfirm = { taskId: null, action: '' };
    },
    taskQuickConfirmMessage(task) {
      if (!task || !task.id) return '';
      if (this.leafConfirm.action === 'cancel') return `取消 #${task.id}？`;
      if (this.leafConfirm.action === 'archive') return `归档 #${task.id}？`;
      if (this.leafConfirm.action === 'delete') return `删除 #${task.id}？`;
      return '';
    },
    requestTaskQuickAction(task, act, ev) {
      if (ev) ev.stopPropagation();
      if (!task || !task.id) return;
      if (!this.taskActionNeedsConfirm(act)) {
        this.clearTaskQuickConfirm();
        this.cp.taskAction(task.id, act);
        return;
      }
      if (this.leafConfirm.taskId === task.id && this.leafConfirm.action === act) {
        this.clearTaskQuickConfirm();
      } else {
        this.leafConfirm = { taskId: task.id, action: act };
      }
    },
    confirmTaskQuickAction(task, ev) {
      if (ev) ev.stopPropagation();
      if (!task || !task.id || !this.leafConfirm.action) return;
      const act = this.leafConfirm.action;
      this.clearTaskQuickConfirm();
      this.cp.taskAction(task.id, act);
    },
  },
  template: `
    <aside class="sidebar">
      <div class="brand-bar">
        <div class="brand">
          <div class="brand-logo">C</div>
          <div>
            <div class="brand-name">CodePilot</div>
            <div class="brand-sub">控制台</div>
          </div>
        </div>
        <button class="icon-btn" @click="cp.toggleDark()" :title="s.dark ? '切换到浅色' : '切换到深色'">
          <svg v-if="!s.dark" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
          <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
        </button>
      </div>

      <div class="sidebar-scroll">
        <div class="group-title">
          <span>项目</span>
          <button class="mini-action" @click="cp.toggleProjectForm()" :title="s.projectForm.open ? '收起' : '新建项目'">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          </button>
        </div>

        <form v-if="s.projectForm.open" class="project-form" @submit.prevent="submitProject">
          <div class="field">
            <label>工作目录</label>
            <input v-model="s.projectForm.path" type="text" placeholder="D:\\myCode\\workflow">
          </div>
          <div class="field">
            <label>项目名</label>
            <input v-model="s.projectForm.name" type="text" placeholder="默认取目录名">
          </div>
          <label class="checkbox">
            <input type="checkbox" v-model="s.projectForm.noConfig">
            不生成 AGENTS.toml
          </label>
          <div class="row gap-xs">
            <button type="submit" class="btn btn-primary btn-sm grow" :disabled="s.projectSubmitting">
              <span v-if="s.projectSubmitting" class="spinner"></span>
              注册
            </button>
            <button type="button" class="btn btn-outline btn-sm" @click="cp.toggleProjectForm(false)" :disabled="s.projectSubmitting">取消</button>
          </div>
        </form>

        <div v-if="!s.projects.length" class="empty-block">还没有项目</div>

        <div v-else class="tree">
          <div v-for="p in s.projects" :key="p.name" class="tree-project">
            <!-- Project node -->
            <div class="tree-row tree-project-row" :class="{active: isActive({project: p.name, view: 'overview'})}" @click="cp.selectProject(p.name)">
              <button class="chevron" @click="toggleProject(p.name, $event)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
                  :style="{transform: isExpanded(p.name) ? 'rotate(90deg)' : 'rotate(0deg)'}">
                  <polyline points="9 18 15 12 9 6"/>
                </svg>
              </button>
              <svg class="tree-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
              <span class="tree-label">{{ p.name }}</span>
              <span v-if="p.stats && p.stats.in_progress" class="chip info tiny">{{ p.stats.in_progress }}</span>
              <span v-else-if="taskCount(p)" class="chip neutral tiny">{{ taskCount(p) }}</span>
              <button class="tree-action" @click="deleteProject(p, $event)" :disabled="s.deletingProject === p.name" title="删除项目">
                <span v-if="s.deletingProject === p.name" class="spinner tiny-spinner"></span>
                <svg v-else width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
              </button>
            </div>

            <!-- Children (only when expanded) -->
            <div v-show="isExpanded(p.name)" class="tree-children">
              <!-- Tasks category -->
              <div class="tree-row tree-category-row" :class="{active: isActive({project: p.name, view: 'tasks'})}" @click="cp.selectCategory(p.name, 'tasks')">
                <button class="chevron" @click="toggleCategory(p.name, 'tasks', $event)">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
                    :style="{transform: isExpanded(p.name + '/tasks') ? 'rotate(90deg)' : 'rotate(0deg)'}">
                    <polyline points="9 18 15 12 9 6"/>
                  </svg>
                </button>
                <svg class="tree-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
                <span class="tree-label">任务</span>
                <span class="muted tiny">{{ taskCount(p) }}</span>
              </div>
              <div v-show="isExpanded(p.name + '/tasks')" class="tree-leaf-wrap">
                <div v-if="!projectTasks(p.name).length" class="tree-empty">暂无任务</div>
                <div v-for="t in visibleLeafItems(projectTasks(p.name), p.name, 'tasks')" :key="t.id"
                     class="tree-row tree-leaf-row"
                     :class="{active: isActive({view: 'task', id: t.id})}"
                     @click="cp.selectTask(p.name, t.id)">
                  <!-- Tasks that hit a preflight skip still show as backlog
                       (待办) in status, but should wear a warning tone here
                       so users spot "this one needs me to act" at a glance
                       instead of blending into the regular gray backlog. -->
                  <span class="dot-status" :class="t.skip_reason ? 'warning' : $cp.toneClass(t.status)"
                        :title="t.skip_reason ? '预检跳过' : $cp.statusLabel(t.status)"></span>
                  <span class="tree-label">
                    <span class="muted tiny">#{{ t.id }}</span> {{ t.title }}
                  </span>
                  <span class="tree-leaf-actions" @click.stop>
                    <button v-if="t.actions.cancel" class="tree-action" title="取消任务"
                            :disabled="cp.isTaskPending(t.id)"
                            @click.stop="requestTaskQuickAction(t, 'cancel', $event)">
                      <span v-if="cp.taskPendingAction(t.id) === 'cancel'" class="spinner tiny-spinner"></span>
                      <svg v-else width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                    </button>
                    <button v-if="t.actions.archive" class="tree-action" title="归档任务"
                            :disabled="cp.isTaskPending(t.id)"
                            @click.stop="requestTaskQuickAction(t, 'archive', $event)">
                      <span v-if="cp.taskPendingAction(t.id) === 'archive'" class="spinner tiny-spinner"></span>
                      <svg v-else width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/><line x1="9" y1="12" x2="15" y2="12"/></svg>
                    </button>
                    <button v-if="t.actions.delete" class="tree-action" title="删除任务"
                            :disabled="cp.isTaskPending(t.id)"
                            @click.stop="requestTaskQuickAction(t, 'delete', $event)">
                      <span v-if="cp.taskPendingAction(t.id) === 'delete'" class="spinner tiny-spinner"></span>
                      <svg v-else width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
                    </button>
                  </span>
                  <div v-if="isTaskQuickConfirm(t)" class="tree-inline-confirm" @click.stop>
                    <span class="tree-inline-confirm-text">{{ taskQuickConfirmMessage(t) }}</span>
                    <button class="btn btn-primary btn-sm"
                            :disabled="cp.isTaskPending(t.id)"
                            @click.stop="confirmTaskQuickAction(t, $event)">
                      确认
                    </button>
                    <button class="btn btn-outline btn-sm"
                            :disabled="cp.isTaskPending(t.id)"
                            @click.stop="clearTaskQuickConfirm()">
                      取消
                    </button>
                  </div>
                </div>
                <button v-if="leafHasMore(projectTasks(p.name), p.name, 'tasks')"
                        class="tree-row tree-more"
                        @click.stop="expandLeaf(p.name, 'tasks')">
                  再展开 {{ leafNextChunk(projectTasks(p.name), p.name, 'tasks') }} 条（剩余 {{ leafRemaining(projectTasks(p.name), p.name, 'tasks') }}）
                </button>
                <button v-if="leafCanCollapse(projectTasks(p.name), p.name, 'tasks')"
                        class="tree-row tree-more"
                        @click.stop="collapseLeaf(p.name, 'tasks')">
                  全部收起
                </button>
              </div>

              <!-- Requirements / jobs category -->
              <div class="tree-row tree-category-row" :class="{active: isActive({project: p.name, view: 'jobs'})}" @click="cp.selectCategory(p.name, 'jobs')">
                <button class="chevron" @click="toggleCategory(p.name, 'jobs', $event)">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
                    :style="{transform: isExpanded(p.name + '/jobs') ? 'rotate(90deg)' : 'rotate(0deg)'}">
                    <polyline points="9 18 15 12 9 6"/>
                  </svg>
                </button>
                <svg class="tree-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span class="tree-label">需求</span>
                <span class="muted tiny">{{ jobCount(p) }}</span>
              </div>
              <div v-show="isExpanded(p.name + '/jobs')" class="tree-leaf-wrap">
                <div v-if="!projectJobs(p.name).length" class="tree-empty">暂无需求</div>
                <div v-for="j in visibleLeafItems(projectJobs(p.name), p.name, 'jobs')" :key="j.id"
                     class="tree-row tree-leaf-row"
                     :class="{active: isActive({view: 'job', id: j.id})}"
                     @click="cp.selectJob(p.name, j.id)">
                  <span v-if="$cp.isJobActive(j)" class="spinner"></span>
                  <span v-else class="dot-status" :class="$cp.toneClass(j.status)"></span>
                  <span class="tree-label">
                    <span class="muted tiny">#{{ j.id }}</span> {{ j.title }}
                  </span>
                </div>
                <button v-if="leafHasMore(projectJobs(p.name), p.name, 'jobs')"
                        class="tree-row tree-more"
                        @click.stop="expandLeaf(p.name, 'jobs')">
                  再展开 {{ leafNextChunk(projectJobs(p.name), p.name, 'jobs') }} 条（剩余 {{ leafRemaining(projectJobs(p.name), p.name, 'jobs') }}）
                </button>
                <button v-if="leafCanCollapse(projectJobs(p.name), p.name, 'jobs')"
                        class="tree-row tree-more"
                        @click.stop="collapseLeaf(p.name, 'jobs')">
                  全部收起
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- Footer: recent events -->
        <div class="side-group" style="margin-top: 18px">
          <div class="group-title"><span>最近事件</span></div>
          <div v-if="s.events.length" class="side-events">
            <div v-for="(ev, i) in s.events" :key="i" class="event-card">
              <div class="event-head">
                <cp-chip :tone="$cp.toneClass(ev.level || 'info')">{{ ev.level || 'info' }}</cp-chip>
                <span class="muted tiny">{{ $cp.fmtTime(ev.time) }}</span>
              </div>
              <div class="tiny">{{ ev.message }}</div>
            </div>
          </div>
          <div v-else class="empty-block">暂无事件</div>
        </div>
      </div>
    </aside>
  `,
});

