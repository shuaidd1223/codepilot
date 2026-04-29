/* List all sessions for current project. */
/* global Vue, CP */
CP.Components.SessionsView = Vue.defineComponent({
  name: 'CpSessionsView',
  inject: ['cp'],
  data() {
    return {
      pageSize: 10,
      visibleCount: 10,
      searchQuery: '',
      searchResults: [],
      searchLoading: false,
      searchSearched: false,
      searchTimer: 0,
    };
  },
  computed: {
    s() { return this.cp.state; },
    sessions() {
      return this.s.sessions.filter(x => !this.s.nav.project || x.project === this.s.nav.project);
    },
    displaySessions() {
      return this.searchSearched ? this.searchResults : this.sessions;
    },
    visibleSessions() {
      return this.displaySessions.slice(0, this.visibleCount);
    },
    hasMore() {
      return this.displaySessions.length > this.visibleCount;
    },
    canCollapse() {
      return this.displaySessions.length > this.pageSize && this.visibleCount > this.pageSize;
    },
    remainingCount() {
      return Math.max(this.displaySessions.length - this.visibleCount, 0);
    },
    nextChunkCount() {
      return Math.min(this.pageSize, this.remainingCount);
    },
  },
  watch: {
    'cp.state.nav.project'() {
      this.visibleCount = this.pageSize;
      if (this.searchQuery.trim()) this.runSearch();
    },
    displaySessions(nextSessions) {
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
    clearSearch() {
      this.searchQuery = '';
      this.searchResults = [];
      this.searchSearched = false;
      this.visibleCount = this.pageSize;
      if (this.searchTimer) {
        clearTimeout(this.searchTimer);
        this.searchTimer = 0;
      }
    },
    scheduleSearch() {
      if (this.searchTimer) clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => {
        this.searchTimer = 0;
        this.runSearch();
      }, 240);
    },
    async runSearch() {
      const query = this.searchQuery.trim();
      this.visibleCount = this.pageSize;
      if (!query) {
        this.searchResults = [];
        this.searchSearched = false;
        return;
      }
      this.searchLoading = true;
      try {
        const qs = new URLSearchParams({
          project: this.s.nav.project || '',
          q: query,
          limit: '80',
        });
        const data = await CP.api.get(`/api/sessions?${qs.toString()}`);
        if (this.searchQuery.trim() !== query) return;
        this.searchResults = data.sessions || [];
        this.searchSearched = true;
      } catch (err) {
        this.cp.pushToast(err.message, 'error');
      } finally {
        if (this.searchQuery.trim() === query) this.searchLoading = false;
      }
    },
  },
  beforeUnmount() {
    if (this.searchTimer) clearTimeout(this.searchTimer);
  },
  template: `
    <div class="view">
      <div class="row between end">
        <div>
          <h2 class="view-title">会话</h2>
          <div class="muted tiny">
            <span v-if="searchSearched">命中 {{ displaySessions.length }} / 共 {{ sessions.length }} 条</span>
            <span v-else>共 {{ sessions.length }} 条</span>
          </div>
        </div>
        <button class="btn btn-primary" @click="cp.newSession()" :disabled="s.newSessionLoading">
          <span v-if="s.newSessionLoading" class="spinner"></span>
          <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          新建会话
        </button>
      </div>

      <div class="session-search-bar">
        <div class="session-search-input">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
          <input
            v-model="searchQuery"
            @input="scheduleSearch"
            @keydown.enter.prevent="runSearch"
            placeholder="搜索会话标题、消息正文或任务编号"
          >
          <span v-if="searchLoading" class="spinner tiny-spinner"></span>
          <button v-if="searchQuery" class="icon-btn small" @click="clearSearch" title="清空搜索">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button>
        </div>
      </div>

      <div v-if="!sessions.length && !searchSearched" class="empty pad">还没有会话，点击右上角「新建会话」开始</div>
      <div v-else-if="searchSearched && !displaySessions.length" class="empty pad">没有找到匹配的会话</div>
      <div v-else class="cards-grid">
        <button v-for="se in visibleSessions" :key="se.id" class="item-card" @click="cp.selectSession(se.project, se.id)">
          <div class="row between">
            <strong class="truncate">{{ se.title }}</strong>
            <span class="muted tiny">#{{ se.id }}</span>
          </div>
          <div class="muted tiny">{{ se.message_count || 0 }} 条消息 · 更新 {{ $cp.fmtTime(se.updated_at) }}</div>
          <div v-if="se.snippet" class="session-snippet">{{ se.snippet }}</div>
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

