/* <cp-agent-log :text="s.taskLog.text" :done="s.taskLog.done" tall follow>
 *
 * Markdown-first live log renderer.
 * Rendering pipeline and interaction-state mechanics are split into:
 * - CP.AgentLogRenderBoundary
 * - CP.AgentLogInteractionBoundary
 */
/* global Vue, CP */

const AgentLogRender = CP.AgentLogRenderBoundary || {};
const AgentLogInteraction = CP.AgentLogInteractionBoundary || {};

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
      if (AgentLogRender.parseMarkdownBlocks) {
        return AgentLogRender.parseMarkdownBlocks(raw, (chunk) => this._renderMarkdown(chunk));
      }
      if (!raw) return [];
      return [{
        type: 'markdown',
        key: `md:0:0:${raw.length}`,
        raw,
        lineCount: this.lineCount,
        html: this._renderMarkdown(raw),
        section: 'other',
      }];
    },
    searchMatches() {
      if (AgentLogInteraction.findSearchMatches) {
        return AgentLogInteraction.findSearchMatches(this.blocks, this.searchQuery);
      }
      return [];
    },
    searchMatchSet() {
      return new Set(this.searchMatches);
    },
    activeSearchIndex() {
      if (this.searchPos < 0 || this.searchPos >= this.searchMatches.length) return -1;
      return this.searchMatches[this.searchPos];
    },
    searchSummary() {
      if (AgentLogInteraction.searchSummary) {
        return AgentLogInteraction.searchSummary(this.searchQuery, this.searchMatches, this.searchPos);
      }
      const hasQuery = !!String(this.searchQuery || '').trim();
      if (!hasQuery) return '搜索';
      const total = this.searchMatches.length;
      if (!total) return '0/0';
      const cur = this.searchPos >= 0 ? this.searchPos + 1 : 0;
      return `${cur}/${total}`;
    },
    linesLabel() {
      if (AgentLogInteraction.linesLabel) return AgentLogInteraction.linesLabel(this.lineCount);
      return `${this.lineCount} 行`;
    },
    jumpLabel() {
      if (AgentLogInteraction.jumpLabel) return AgentLogInteraction.jumpLabel(this.unreadLines);
      return '↓ 回到最新';
    },
  },
  watch: {
    textLength() {
      if (AgentLogInteraction.handleTextLengthChanged) {
        AgentLogInteraction.handleTextLengthChanged(this);
        return;
      }
      this.$nextTick(() => {
        if (!this.textLength) this.unreadLines = 0;
        if (this.followEnabled && this.stickToBottom) this._scrollToBottom();
        this._scheduleEnhance();
      });
    },
    lineCount(next, prev) {
      if (AgentLogInteraction.handleLineCountChanged) {
        AgentLogInteraction.handleLineCountChanged(this, next, prev);
      } else {
        const oldVal = Number.isFinite(prev) ? prev : 0;
        const delta = Math.max(0, (Number.isFinite(next) ? next : 0) - oldVal);
        if (!delta) return;
        if (!this.followEnabled || !this.stickToBottom) this.unreadLines += delta;
      }
    },
    searchQuery() {
      if (AgentLogInteraction.handleSearchQueryChanged) {
        AgentLogInteraction.handleSearchQueryChanged(this);
      } else {
        this.searchPos = -1;
      }
    },
    searchMatches(next) {
      if (AgentLogInteraction.handleSearchMatchesChanged) {
        AgentLogInteraction.handleSearchMatchesChanged(this, next);
      } else {
        const len = Array.isArray(next) ? next.length : 0;
        if (!len) {
          this.searchPos = -1;
          return;
        }
        if (this.searchPos >= len) this.searchPos = 0;
      }
    },
    blocks() {
      this.$nextTick(() => this._scheduleEnhance());
    },
  },
  created() {
    this._mdCache = AgentLogRender.createMarkdownCache
      ? AgentLogRender.createMarkdownCache()
      : new Map();
    this._enhanceRaf = 0;
  },
  mounted() {
    const body = this.$refs.body;
    if (!body) return;
    body.addEventListener('scroll', this._onScroll, { passive: true });
    this.$nextTick(() => {
      this._scrollToBottom();
      this._scheduleEnhance();
    });
  },
  beforeUnmount() {
    const body = this.$refs.body;
    if (body) body.removeEventListener('scroll', this._onScroll);
    if (this._enhanceRaf) cancelAnimationFrame(this._enhanceRaf);
  },
  methods: {
    _renderMarkdown(raw) {
      if (AgentLogRender.renderMarkdown) {
        return AgentLogRender.renderMarkdown(this._mdCache, raw);
      }
      const text = String(raw || '');
      return (CP.renderOutput
        ? CP.renderOutput(text)
        : CP.escapeHtml(text).replace(/\n/g, '<br>'));
    },

    toggleFollow() {
      if (AgentLogInteraction.toggleFollow) {
        AgentLogInteraction.toggleFollow(this);
      } else {
        this.manualFollowPaused = !this.manualFollowPaused;
        if (!this.manualFollowPaused) this.scrollToBottom();
      }
    },
    onSearchKeydown(ev) {
      if (AgentLogInteraction.onSearchKeydown) {
        AgentLogInteraction.onSearchKeydown(this, ev);
      } else if (ev && ev.key === 'Enter') {
        if (ev.shiftKey) this.prevMatch();
        else this.nextMatch();
      }
    },
    nextMatch() {
      if (AgentLogInteraction.nextMatch) {
        AgentLogInteraction.nextMatch(this);
      } else {
        const total = this.searchMatches.length;
        if (!total) return;
        const next = (this.searchPos + 1 + total) % total;
        this.searchPos = next;
        this.scrollToBlock(this.searchMatches[next]);
      }
    },
    prevMatch() {
      if (AgentLogInteraction.prevMatch) {
        AgentLogInteraction.prevMatch(this);
      } else {
        const total = this.searchMatches.length;
        if (!total) return;
        const prev = (this.searchPos - 1 + total) % total;
        this.searchPos = prev;
        this.scrollToBlock(this.searchMatches[prev]);
      }
    },
    scrollToBlock(idx) {
      if (AgentLogInteraction.scrollToBlock) {
        AgentLogInteraction.scrollToBlock(this, idx);
      } else {
        const body = this.$refs.body;
        if (!body || !Number.isFinite(idx) || idx < 0) return;
        const node = body.querySelector(`.al-md-wrap[data-idx="${idx}"]`);
        if (!node) return;
        this.manualFollowPaused = true;
        this.stickToBottom = false;
        node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    },
    isSearchHit(idx) {
      return this.searchMatchSet.has(idx);
    },
    isSearchActive(idx) {
      return this.activeSearchIndex === idx;
    },

    _scheduleEnhance() {
      if (AgentLogRender.scheduleEnhance) {
        AgentLogRender.scheduleEnhance(this, () => this._enhanceCodeBlocks());
        return;
      }
      if (this._enhanceRaf) return;
      this._enhanceRaf = requestAnimationFrame(() => {
        this._enhanceRaf = 0;
        this._enhanceCodeBlocks();
      });
    },
    _enhanceCodeBlocks() {
      const body = this.$refs.body;
      if (!body) return;
      if (AgentLogRender.enhanceCodeBlocks) {
        AgentLogRender.enhanceCodeBlocks(body);
      }
    },

    _scrollToBottom() {
      const body = this.$refs.body;
      if (!body) return;
      body.scrollTop = body.scrollHeight;
    },
    _onScroll() {
      if (AgentLogInteraction.onScroll) {
        AgentLogInteraction.onScroll(this);
      } else {
        const body = this.$refs.body;
        if (!body) return;
        const distance = body.scrollHeight - body.scrollTop - body.clientHeight;
        this.stickToBottom = distance < 24;
        if (this.stickToBottom) this.unreadLines = 0;
      }
    },
    scrollToBottom() {
      if (AgentLogInteraction.scrollToBottom) {
        AgentLogInteraction.scrollToBottom(this);
      } else {
        this.manualFollowPaused = false;
        this.stickToBottom = true;
        this.unreadLines = 0;
        this.$nextTick(() => this._scrollToBottom());
      }
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
            :data-idx="idx"
            :data-section="b.section || 'other'">
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
