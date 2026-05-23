/* <cp-agent-log :text="s.taskLog.text" :done="s.taskLog.done" tall follow>
 *
 * Markdown-first live log renderer.
 * Rendering + interaction are bridged through CP.AgentLogBoundaryContract
 * so component code only speaks one stable adapter contract.
 */
/* global Vue, CP */

const AgentLogAdapter = CP.AgentLogBoundaryContract.createAdapter({
  renderBoundary: CP.AgentLogRenderBoundary,
  interactionBoundary: CP.AgentLogInteractionBoundary,
});
const AgentLogRender = AgentLogAdapter.render;
const AgentLogInteraction = AgentLogAdapter.interaction;

CP.Components.AgentLog = Vue.defineComponent({
  name: 'CpAgentLog',
  props: {
    text: { type: String, default: '' },
    tall: Boolean,
    follow: { type: Boolean, default: true },
    done: { type: Boolean, default: true },
    title: { type: String, default: 'agent · live tail' },
  },
  data() {
    return {
      stickToBottom: true,
      manualFollowPaused: false,
      unreadLines: 0,
      searchQuery: '',
      searchPos: -1,
    };
  },
  computed: {
    textLength() { return (this.text || '').length; },
    lineCount() {
      if (!this.text) return 0;
      return this.text.split(/\r?\n/).length;
    },
    followEnabled() {
      return !!this.follow && !this.manualFollowPaused;
    },
    blocks() {
      const raw = this.text || '';
      return AgentLogRender.parseMarkdownBlocks(raw, (chunk) => this._renderMarkdown(chunk));
    },
    searchMatches() {
      return AgentLogInteraction.findSearchMatches(this.blocks, this.searchQuery);
    },
    searchMatchSet() {
      return new Set(this.searchMatches);
    },
    activeSearchIndex() {
      if (this.searchPos < 0 || this.searchPos >= this.searchMatches.length) return -1;
      return this.searchMatches[this.searchPos];
    },
    searchSummary() {
      return AgentLogInteraction.searchSummary(this.searchQuery, this.searchMatches, this.searchPos);
    },
    linesLabel() {
      return AgentLogInteraction.linesLabel(this.lineCount);
    },
    jumpLabel() {
      return AgentLogInteraction.jumpLabel(this.unreadLines);
    },
  },
  watch: {
    textLength() {
      AgentLogInteraction.handleTextLengthChanged(this);
    },
    lineCount(next, prev) {
      AgentLogInteraction.handleLineCountChanged(this, next, prev);
    },
    searchQuery() {
      AgentLogInteraction.handleSearchQueryChanged(this);
    },
    searchMatches(next) {
      AgentLogInteraction.handleSearchMatchesChanged(this, next);
    },
  },
  created() {
    this._mdCache = AgentLogRender.createMarkdownCache();
  },
  mounted() {
    const body = this.$refs.body;
    if (!body) return;
    body.addEventListener('scroll', this._onScroll, { passive: true });
    this.$nextTick(() => {
      this._scrollToBottom();
    });
  },
  beforeUnmount() {
    const body = this.$refs.body;
    if (body) body.removeEventListener('scroll', this._onScroll);
  },
  methods: {
    _renderMarkdown(raw) {
      return AgentLogRender.renderMarkdown(this._mdCache, raw);
    },

    toggleFollow() {
      AgentLogInteraction.toggleFollow(this);
    },
    onSearchKeydown(ev) {
      AgentLogInteraction.onSearchKeydown(this, ev);
    },
    nextMatch() {
      AgentLogInteraction.nextMatch(this);
    },
    prevMatch() {
      AgentLogInteraction.prevMatch(this);
    },
    scrollToBlock(idx) {
      AgentLogInteraction.scrollToBlock(this, idx);
    },
    isSearchHit(idx) {
      return this.searchMatchSet.has(idx);
    },
    isSearchActive(idx) {
      return this.activeSearchIndex === idx;
    },

    _scrollToBottom() {
      const body = this.$refs.body;
      if (!body) return;
      body.scrollTop = body.scrollHeight;
    },
    _onScroll() {
      AgentLogInteraction.onScroll(this);
    },
    scrollToBottom() {
      AgentLogInteraction.scrollToBottom(this);
    },
  },
  template: `
    <div class="agent-log" :class="[tall ? 'tall' : '', done ? '' : 'streaming']">
      <div class="agent-log-head">
        <span class="dots"><i></i><i></i><i></i></span>
        <span class="title">{{ title }}</span>
        <span class="spacer"></span>
        <div class="al-search-row">
          <input
            v-model="searchQuery"
            class="al-search-input"
            type="text"
            placeholder="搜索日志…"
            @keydown="onSearchKeydown" />
          <button class="al-search-btn" :disabled="!searchMatches.length" @click="prevMatch" title="上一个命中 (Shift+Enter)">↑</button>
          <button class="al-search-btn" :disabled="!searchMatches.length" @click="nextMatch" title="下一个命中 (Enter)">↓</button>
          <span class="al-search-stat">{{ searchSummary }}</span>
        </div>
        <button
          class="al-head-btn"
          :class="{ active: !manualFollowPaused }"
          @click="toggleFollow"
          :title="manualFollowPaused ? '恢复自动跟随新日志' : '暂停自动跟随，便于回看'">
          {{ manualFollowPaused ? '恢复跟随' : '暂停跟随' }}
        </button>
        <span v-if="!done" class="streaming-pill">streaming…</span>
        <span class="lines">{{ linesLabel }}</span>
      </div>

      <div class="agent-log-bodywrap">
        <div ref="body" class="agent-log-body">
          <div
            v-for="(b, idx) in blocks"
            :key="b.key"
            class="al-md-wrap"
            :class="{ 'is-hit': isSearchHit(idx), 'is-active': isSearchActive(idx) }"
            :data-idx="idx">
            <div class="al-block al-text al-text-md al-md-block">
              <div class="md" v-html="b.html"></div>
            </div>
          </div>

          <div v-if="!blocks.length" class="al-empty">暂无日志</div>
          <span v-if="!done" class="al-cursor"></span>
        </div>

        <button
          v-if="!stickToBottom || manualFollowPaused"
          class="agent-log-jump"
          @click="scrollToBottom"
          title="回到最新并恢复自动跟随">
          {{ jumpLabel }}
        </button>
      </div>
    </div>
  `,
});
