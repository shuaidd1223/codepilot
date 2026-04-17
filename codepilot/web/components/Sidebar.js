/* Project-first tree sidebar: projects → (sessions, tasks, jobs). */
/* global Vue, CP */
CP.Components.Sidebar = Vue.defineComponent({
  name: 'CpSidebar',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    allSessions() { return this.s.sessions || []; },
  },
  methods: {
    isExpanded(key, def) { return this.cp.isExpanded(key, def); },
    toggleProject(name, ev) {
      ev.stopPropagation();
      this.cp.toggleProject(name);
    },
    toggleCategory(project, cat, ev) {
      ev.stopPropagation();
      const key = project + '/' + cat;
      this.cp.toggleExpanded(key);
    },
    isActive(match) {
      const n = this.s.nav;
      return Object.keys(match).every(k => n[k] === match[k]);
    },
    projectSessions(projName) {
      return this.allSessions.filter(se => se.project === projName);
    },
    projectTasks(projName) {
      /* tasks only loaded for current project via loadDashboard */
      return projName === this.s.nav.project ? (this.s.tasks || []) : [];
    },
    projectJobs(projName) {
      return projName === this.s.nav.project ? (this.s.jobs || []) : [];
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
        <div class="group-title"><span>项目</span><span class="muted">{{ s.projects.length }}</span></div>

        <div v-if="!s.projects.length" class="empty-block">还没有项目，先执行 codepilot init</div>

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
              <span v-else-if="p.stats && p.stats.total" class="chip neutral tiny">{{ p.stats.total }}</span>
            </div>

            <!-- Children (only when expanded) -->
            <div v-show="isExpanded(p.name)" class="tree-children">
              <!-- Sessions category -->
              <div class="tree-row tree-category-row" :class="{active: isActive({project: p.name, view: 'sessions'})}" @click="cp.selectCategory(p.name, 'sessions')">
                <button class="chevron" @click="toggleCategory(p.name, 'sessions', $event)">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
                    :style="{transform: isExpanded(p.name + '/sessions') ? 'rotate(90deg)' : 'rotate(0deg)'}">
                    <polyline points="9 18 15 12 9 6"/>
                  </svg>
                </button>
                <svg class="tree-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                <span class="tree-label">会话</span>
                <span class="muted tiny">{{ projectSessions(p.name).length }}</span>
              </div>
              <div v-show="isExpanded(p.name + '/sessions')" class="tree-leaf-wrap">
                <div v-if="!projectSessions(p.name).length" class="tree-empty">暂无会话</div>
                <div v-for="se in projectSessions(p.name)" :key="se.id"
                     class="tree-row tree-leaf-row"
                     :class="{active: isActive({view: 'session', id: se.id})}"
                     @click="cp.selectSession(p.name, se.id)">
                  <span class="tree-label">
                    <span class="muted tiny">#{{ se.id }}</span> {{ se.title }}
                  </span>
                  <span class="muted tiny">{{ se.message_count || 0 }}</span>
                </div>
                <button class="tree-row tree-add" @click="cp.newSession()">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
                  新建会话
                </button>
              </div>

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
                <span class="muted tiny">{{ projectTasks(p.name).length }}</span>
              </div>
              <div v-show="isExpanded(p.name + '/tasks')" class="tree-leaf-wrap">
                <div v-if="!projectTasks(p.name).length" class="tree-empty">暂无任务</div>
                <div v-for="t in projectTasks(p.name)" :key="t.id"
                     class="tree-row tree-leaf-row"
                     :class="{active: isActive({view: 'task', id: t.id})}"
                     @click="cp.selectTask(p.name, t.id)">
                  <span class="dot-status" :class="$cp.toneClass(t.status)"></span>
                  <span class="tree-label">
                    <span class="muted tiny">#{{ t.id }}</span> {{ t.title }}
                  </span>
                </div>
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
                <span class="muted tiny">{{ projectJobs(p.name).length }}</span>
              </div>
              <div v-show="isExpanded(p.name + '/jobs')" class="tree-leaf-wrap">
                <div v-if="!projectJobs(p.name).length" class="tree-empty">暂无需求</div>
                <div v-for="j in projectJobs(p.name)" :key="j.id"
                     class="tree-row tree-leaf-row"
                     :class="{active: isActive({view: 'job', id: j.id})}"
                     @click="cp.selectJob(p.name, j.id)">
                  <span v-if="$cp.isJobActive(j)" class="spinner"></span>
                  <span v-else class="dot-status" :class="$cp.toneClass(j.status)"></span>
                  <span class="tree-label">
                    <span class="muted tiny">#{{ j.id }}</span> {{ j.title }}
                  </span>
                </div>
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
