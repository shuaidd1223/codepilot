/* List all sessions for current project. */
/* global Vue, CP */
CP.Components.SessionsView = Vue.defineComponent({
  name: 'CpSessionsView',
  inject: ['cp'],
  data() {
    return {
      pageSize: 10,
      visibleCount: 10,
    };
  },
  computed: {
    s() { return this.cp.state; },
    sessions() {
      return this.s.sessions.filter(x => !this.s.nav.project || x.project === this.s.nav.project);
    },
    visibleSessions() {
      return this.sessions.slice(0, this.visibleCount);
    },
    hasMore() {
      return this.sessions.length > this.visibleCount;
    },
    canCollapse() {
      return this.sessions.length > this.pageSize && this.visibleCount > this.pageSize;
    },
    remainingCount() {
      return Math.max(this.sessions.length - this.visibleCount, 0);
    },
    nextChunkCount() {
      return Math.min(this.pageSize, this.remainingCount);
    },
  },
  watch: {
    'cp.state.nav.project'() {
      this.visibleCount = this.pageSize;
    },
    sessions(nextSessions) {
      if (!Array.isArray(nextSessions)) {
        this.visibleCount = this.pageSize;
        return;
      }
      if (nextSessions.length <= this.pageSize) {
        this.visibleCount = this.pageSize;
        return;
      }
      if (this.visibleCount > nextSessions.length) {
        this.visibleCount = Math.max(this.pageSize, nextSessions.length);
      }
    },
  },
  methods: {
    loadMore() {
      this.visibleCount += this.pageSize;
    },
    collapseList() {
      this.visibleCount = this.pageSize;
    },
  },
  template: `
    <div class="view">
      <div class="row between end">
        <div>
          <h2 class="view-title">会话</h2>
          <div class="muted tiny">共 {{ sessions.length }} 条</div>
        </div>
        <button class="btn btn-primary" @click="cp.newSession()" :disabled="s.newSessionLoading">
          <span v-if="s.newSessionLoading" class="spinner"></span>
          <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          新建会话
        </button>
      </div>

      <div v-if="!sessions.length" class="empty pad">还没有会话，点击右上角「新建会话」开始</div>
      <div v-else class="cards-grid">
        <button v-for="se in visibleSessions" :key="se.id" class="item-card" @click="cp.selectSession(se.project, se.id)">
          <div class="row between">
            <strong class="truncate">{{ se.title }}</strong>
            <span class="muted tiny">#{{ se.id }}</span>
          </div>
          <div class="muted tiny">{{ se.message_count || 0 }} 条消息 · 更新 {{ $cp.fmtTime(se.updated_at) }}</div>
          <div v-if="se.project !== s.nav.project" class="chip neutral tiny">{{ se.project }}</div>
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

