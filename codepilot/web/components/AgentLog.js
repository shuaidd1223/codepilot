/* <cp-agent-log :text="s.taskLog.text" :done="s.taskLog.done" tall follow>
 *
 * Text-first live log renderer.
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
    phase: { type: String, default: '' },
    agent: { type: String, default: '' },
    review: { type: Object, default: null },
  },
  data() {
    return {
      stickToBottom: true,
      manualFollowPaused: false,
      unreadLines: 0,
      searchQuery: '',
      searchPos: -1,
      expandedBlocks: {},
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
      return AgentLogRender.parseMarkdownBlocks(raw, {
        phase: this.phase,
        agent: this.agent,
        review: this.review,
      });
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
  mounted() {
    const body = this.$refs.body;
    if (!body) return;
    body.addEventListener('scroll', this._onScroll, { passive: true });
    this.$nextTick(() => {
      if (this.followEnabled) this._scrollToBottom();
    });
  },
  beforeUnmount() {
    const body = this.$refs.body;
    if (body) body.removeEventListener('scroll', this._onScroll);
  },
  methods: {
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
    blockClass(block, idx) {
      return [
        'type-' + ((block && block.type) || 'log'),
        {
          'is-hit': this.isSearchHit(idx),
          'is-active': this.isSearchActive(idx),
        },
      ];
    },
    isBlockExpanded(key, block = null) {
      if (Object.prototype.hasOwnProperty.call(this.expandedBlocks, key)) {
        return !!this.expandedBlocks[key];
      }
      return !!(block && block.collapsed === false);
    },
    toggleBlock(key, block = null) {
      this.expandedBlocks = {
        ...this.expandedBlocks,
        [key]: !this.isBlockExpanded(key, block),
      };
    },
    isBlockCollapsible(block) {
      return !!(block && block.collapsed);
    },
    foldCaret(key, block = null) {
      return this.isBlockExpanded(key, block) ? '⌄' : '›';
    },
    blockTitle(block) {
      if (!block) return '日志';
      return block.title || `${block.lineCount || 0} 行输出`;
    },
    blockMeta(block) {
      if (!block) return '';
      return `${block.lineCount || 0} 行`;
    },
    commandGroupTitle(block) {
      const count = block && block.commandCount ? block.commandCount : 0;
      const failed = block && block.failedCount ? block.failedCount : 0;
      if (failed) return `已运行 ${count} 条命令 · ${failed} 条失败`;
      return `已运行 ${count} 条命令`;
    },
    commandRunTitle(run, idx) {
      return `命令 ${idx + 1}`;
    },
    commandRunTone(run) {
      return run && run.tone ? `tone-${run.tone}` : 'tone-neutral';
    },
    verdictLabel(block) {
      const verdict = String((block && block.verdict) || 'unknown').toUpperCase();
      return `VERDICT: ${verdict}`;
    },
    reviewTone(block) {
      const verdict = String((block && block.verdict) || '').toLowerCase();
      if (verdict === 'pass') return 'tone-pass';
      if (verdict === 'fail') return 'tone-fail';
      return 'tone-unknown';
    },
    reviewSource(block) {
      const source = String((block && block.source) || '').trim();
      return source ? `source=${source}` : '';
    },
    listCount(list) {
      return Array.isArray(list) ? list.length : 0;
    },
    telemetryTone(block) {
      return block && block.tone ? `tone-${block.tone}` : 'tone-neutral';
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
            class="al-log-wrap"
            :class="blockClass(b, idx)"
            :data-idx="idx">
            <template v-if="b.type === 'review-verdict'">
              <div class="al-block al-review-card" :class="reviewTone(b)">
                <div class="al-review-head">
                  <span class="al-review-title">Review 结论</span>
                  <span class="al-review-badge">{{ verdictLabel(b) }}</span>
                  <span v-if="reviewSource(b)" class="al-review-source">{{ reviewSource(b) }}</span>
                </div>
                <table v-if="b.acChecks && b.acChecks.length" class="al-review-table">
                  <thead>
                    <tr><th>AC</th><th>状态</th><th>说明</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(ac, acIdx) in b.acChecks" :key="acIdx">
                      <td>{{ ac.id || '-' }}</td>
                      <td><span class="al-review-status">{{ ac.status || '-' }}</span></td>
                      <td>{{ ac.reason || '-' }}</td>
                    </tr>
                  </tbody>
                </table>
                <div v-if="listCount(b.blockers)" class="al-review-section">
                  <div class="al-review-section-title">需要处理</div>
                  <ul>
                    <li v-for="(item, itemIdx) in b.blockers" :key="'b' + itemIdx">{{ item }}</li>
                  </ul>
                </div>
                <div v-if="listCount(b.advisory)" class="al-review-section">
                  <div class="al-review-section-title">非阻塞观察</div>
                  <ul>
                    <li v-for="(item, itemIdx) in b.advisory" :key="'a' + itemIdx">{{ item }}</li>
                  </ul>
                </div>
              </div>
            </template>
            <template v-else-if="b.type === 'run-meta'">
              <div class="al-block al-run-meta">
                <span class="al-run-meta-title">{{ b.title || '运行信息' }}</span>
                <span class="al-run-meta-summary">{{ b.summary || b.raw }}</span>
              </div>
            </template>
            <template v-else-if="b.type === 'telemetry'">
              <div class="al-block al-telemetry" :class="telemetryTone(b)">
                <span class="al-telemetry-title">{{ b.title || '执行器事件' }}</span>
                <span class="al-telemetry-summary">{{ b.summary || b.raw }}</span>
              </div>
            </template>
            <template v-else-if="b.type === 'command-group'">
              <button
                type="button"
                class="al-fold-row al-command-group-head"
                :aria-expanded="isBlockExpanded(b.key, b)"
                @click="toggleBlock(b.key, b)">
                <span class="al-fold-caret">{{ foldCaret(b.key, b) }}</span>
                <span class="al-fold-title">{{ commandGroupTitle(b) }}</span>
                <span class="al-fold-meta">{{ blockMeta(b) }}</span>
              </button>
              <div v-if="isBlockExpanded(b.key, b)" class="al-command-list">
                <div v-for="(run, runIdx) in b.runs" :key="run.key" class="al-command-run">
                  <button
                    type="button"
                    class="al-fold-row al-command-run-head"
                    :aria-expanded="isBlockExpanded(run.key, run)"
                    @click="toggleBlock(run.key, run)">
                    <span class="al-fold-caret">{{ foldCaret(run.key, run) }}</span>
                    <span class="al-fold-title">{{ commandRunTitle(run, runIdx) }}</span>
                    <span class="al-command-text">{{ run.command }}</span>
                    <span v-if="run.status" class="al-command-status" :class="commandRunTone(run)">{{ run.status }}</span>
                    <span class="al-fold-meta">{{ blockMeta(run) }}</span>
                  </button>
                  <div v-if="isBlockExpanded(run.key, run)" class="al-block al-log-block al-command-run-body">
                    <pre class="al-log-text" v-text="run.raw"></pre>
                  </div>
                </div>
              </div>
            </template>
            <template v-else>
              <button
                v-if="isBlockCollapsible(b)"
                type="button"
                class="al-fold-row al-log-summary"
                :aria-expanded="isBlockExpanded(b.key, b)"
                @click="toggleBlock(b.key, b)">
                <span class="al-fold-caret">{{ foldCaret(b.key, b) }}</span>
                <span class="al-fold-title">{{ blockTitle(b) }}</span>
                <span class="al-fold-meta">{{ blockMeta(b) }}</span>
              </button>
              <div v-if="!isBlockCollapsible(b) || isBlockExpanded(b.key, b)" class="al-block al-log-block">
                <pre class="al-log-text" v-text="b.raw"></pre>
              </div>
            </template>
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
