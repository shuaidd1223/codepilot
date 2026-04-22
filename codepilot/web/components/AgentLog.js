/* <cp-agent-log :text="s.taskLog.text" :done="s.taskLog.done" tall follow>
 *
 * Markdown-first live log renderer.
 * Logs under runs/*.md and *.console.md are treated as canonical markdown,
 * with minimal UI affordances (follow/search/collapsible code blocks).
 */
/* global Vue, CP */

const SECTION_HEAD_RE = /^##\s+/;
const LIVE_HEAD_RE = /^##\s+Live Output\s*$/i;
const FENCE_RE = /^\s*(```+|~~~+)/;
const COLLAPSE_LANG_RE = /\blanguage-(shell|bash|sh|powershell|ps1|cmd|zsh|console)\b/i;
const DIFF_LANG_RE = /\blanguage-diff\b/i;
const CACHE_MAX_ENTRIES = 260;

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
      return this._parseMarkdownBlocks(this.text || '');
    },
    searchMatches() {
      const q = String(this.searchQuery || '').trim().toLowerCase();
      if (!q) return [];
      const out = [];
      for (let i = 0; i < this.blocks.length; i++) {
        const raw = String((this.blocks[i] && this.blocks[i].raw) || '').toLowerCase();
        if (raw.includes(q)) out.push(i);
      }
      return out;
    },
    searchMatchSet() {
      return new Set(this.searchMatches);
    },
    activeSearchIndex() {
      if (this.searchPos < 0 || this.searchPos >= this.searchMatches.length) return -1;
      return this.searchMatches[this.searchPos];
    },
    searchSummary() {
      const hasQuery = !!String(this.searchQuery || '').trim();
      if (!hasQuery) return '搜索';
      const total = this.searchMatches.length;
      if (!total) return '0/0';
      const cur = this.searchPos >= 0 ? this.searchPos + 1 : 0;
      return `${cur}/${total}`;
    },
    linesLabel() {
      return `${this.lineCount} 行`;
    },
    jumpLabel() {
      return this.unreadLines > 0 ? `↓ 回到最新 · +${this.unreadLines} 行` : '↓ 回到最新';
    },
  },
  watch: {
    textLength() {
      this.$nextTick(() => {
        if (!this.textLength) this.unreadLines = 0;
        if (this.followEnabled && this.stickToBottom) this._scrollToBottom();
        this._scheduleEnhance();
      });
    },
    lineCount(next, prev) {
      const oldVal = Number.isFinite(prev) ? prev : 0;
      const delta = Math.max(0, (Number.isFinite(next) ? next : 0) - oldVal);
      if (!delta) return;
      if (!this.followEnabled || !this.stickToBottom) this.unreadLines += delta;
    },
    searchQuery() {
      this.searchPos = -1;
    },
    searchMatches(next) {
      const len = Array.isArray(next) ? next.length : 0;
      if (!len) {
        this.searchPos = -1;
        return;
      }
      if (this.searchPos >= len) this.searchPos = 0;
    },
    blocks() {
      this.$nextTick(() => this._scheduleEnhance());
    },
  },
  created() {
    this._mdCache = new Map();
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
    _cacheGet(raw) {
      if (!this._mdCache.has(raw)) return null;
      const html = this._mdCache.get(raw);
      this._mdCache.delete(raw);
      this._mdCache.set(raw, html);
      return html;
    },
    _cacheSet(raw, html) {
      this._mdCache.set(raw, html);
      while (this._mdCache.size > CACHE_MAX_ENTRIES) {
        const first = this._mdCache.keys().next();
        if (first && !first.done) this._mdCache.delete(first.value);
        else break;
      }
    },
    _renderMarkdown(raw) {
      const key = String(raw || '');
      const cached = this._cacheGet(key);
      if (cached != null) return cached;
      const html = (CP.renderOutput ? CP.renderOutput(key) : CP.escapeHtml(key).replace(/\n/g, '<br>'));
      this._cacheSet(key, html);
      return html;
    },
    _splitSections(lines) {
      if (!lines.length) return [];
      const ranges = [];
      let start = 0;
      for (let i = 1; i < lines.length; i++) {
        if (!SECTION_HEAD_RE.test(String(lines[i] || ''))) continue;
        ranges.push([start, i]);
        start = i;
      }
      ranges.push([start, lines.length]);
      return ranges;
    },
    _chunkSectionLines(lines, isLiveSection) {
      if (!lines.length) return [];
      if (lines.length <= 180) return [lines];

      const chunks = [];
      const limit = isLiveSection ? 110 : 220;
      let cur = [];
      let inFence = false;
      let fenceChar = '';

      const flush = () => {
        if (!cur.length) return;
        chunks.push(cur);
        cur = [];
      };

      const shouldBreak = (idx) => {
        if (inFence) return false;
        if (cur.length < limit) return false;
        const next = String(lines[idx + 1] || '').trim();
        if (!next) return true;
        if (SECTION_HEAD_RE.test(next)) return true;
        if (next.startsWith('### ')) return true;
        return false;
      };

      for (let i = 0; i < lines.length; i++) {
        const line = String(lines[i] || '');
        const trimmed = line.trim();
        const fenceM = FENCE_RE.exec(trimmed);
        if (fenceM) {
          const curFenceChar = fenceM[1][0] || '`';
          if (!inFence) {
            inFence = true;
            fenceChar = curFenceChar;
          } else if (curFenceChar === fenceChar) {
            inFence = false;
          }
        }

        cur.push(line);
        if (shouldBreak(i)) flush();
      }
      flush();
      return chunks;
    },
    _parseMarkdownBlocks(raw) {
      if (!raw) return [];
      const lines = raw.split(/\r?\n/);
      const sections = this._splitSections(lines);
      const out = [];

      for (let s = 0; s < sections.length; s++) {
        const [start, end] = sections[s];
        const secLines = lines.slice(start, end);
        const head = String(secLines[0] || '').trim();
        const isLive = LIVE_HEAD_RE.test(head);
        const chunks = this._chunkSectionLines(secLines, isLive);

        for (let i = 0; i < chunks.length; i++) {
          const chunkLines = chunks[i];
          const rawChunk = chunkLines.join('\n').replace(/\s+$/g, '');
          if (!rawChunk) continue;
          const key = `md:${start}:${i}:${rawChunk.length}`;
          out.push({
            type: 'markdown',
            key,
            raw: rawChunk,
            lineCount: chunkLines.length,
            html: this._renderMarkdown(rawChunk),
          });
        }
      }
      return out;
    },

    toggleFollow() {
      this.manualFollowPaused = !this.manualFollowPaused;
      if (!this.manualFollowPaused) this.scrollToBottom();
    },
    onSearchKeydown(ev) {
      if (!ev || ev.key !== 'Enter') return;
      if (ev.shiftKey) this.prevMatch();
      else this.nextMatch();
    },
    nextMatch() {
      const total = this.searchMatches.length;
      if (!total) return;
      const next = (this.searchPos + 1 + total) % total;
      this.searchPos = next;
      this.scrollToBlock(this.searchMatches[next]);
    },
    prevMatch() {
      const total = this.searchMatches.length;
      if (!total) return;
      const prev = (this.searchPos - 1 + total) % total;
      this.searchPos = prev;
      this.scrollToBlock(this.searchMatches[prev]);
    },
    scrollToBlock(idx) {
      const body = this.$refs.body;
      if (!body || !Number.isFinite(idx) || idx < 0) return;
      const node = body.querySelector(`.al-md-wrap[data-idx="${idx}"]`);
      if (!node) return;
      this.manualFollowPaused = true;
      this.stickToBottom = false;
      node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    },
    isSearchHit(idx) {
      return this.searchMatchSet.has(idx);
    },
    isSearchActive(idx) {
      return this.activeSearchIndex === idx;
    },

    _scheduleEnhance() {
      if (this._enhanceRaf) return;
      this._enhanceRaf = requestAnimationFrame(() => {
        this._enhanceRaf = 0;
        this._enhanceCodeBlocks();
      });
    },
    _enhanceCodeBlocks() {
      const body = this.$refs.body;
      if (!body) return;
      const cards = body.querySelectorAll('.al-md-block .md-code');
      cards.forEach((card) => {
        if (!(card instanceof HTMLElement)) return;
        if (card.dataset.alEnhanced === '1') return;
        card.dataset.alEnhanced = '1';

        const pre = card.querySelector('pre');
        if (!(pre instanceof HTMLElement)) return;
        const source = String(pre.textContent || '').replace(/\s+$/g, '');
        if (!source) return;

        const lines = source.split(/\r?\n/);
        const maxLen = lines.reduce((m, line) => Math.max(m, String(line || '').length), 0);
        const klass = `${pre.className || ''} ${(pre.querySelector('code') && pre.querySelector('code').className) || ''}`;

        const isDiff = DIFF_LANG_RE.test(klass) || /^diff --git\s+/m.test(source);
        const isCommand = COLLAPSE_LANG_RE.test(klass);
        const collapseLine = isCommand && lines.length <= 3 && maxLen > 180;
        const collapseLarge = lines.length > (isDiff ? 90 : 34);
        if (!collapseLine && !collapseLarge) return;

        const collapsedRows = collapseLine ? 1 : (isDiff ? 18 : 14);
        const hiddenRows = Math.max(0, lines.length - collapsedRows);

        card.classList.add('al-md-collapsible');
        card.classList.add(collapseLine ? 'al-md-collapse-line' : 'al-md-collapse-block');
        card.classList.add('is-collapsed');
        const px = Math.max(26, Math.round(collapsedRows * 19 + 16));
        card.style.setProperty('--al-md-collapse-height', `${px}px`);

        const head = card.querySelector('.md-code-head');
        if (!(head instanceof HTMLElement)) return;

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'al-md-toggle';
        btn.textContent = hiddenRows > 0 ? `展开 ${hiddenRows} 行` : '展开';
        btn.addEventListener('click', () => {
          const expanded = card.classList.toggle('is-expanded');
          card.classList.toggle('is-collapsed', !expanded);
          if (expanded) btn.textContent = '收起';
          else btn.textContent = hiddenRows > 0 ? `展开 ${hiddenRows} 行` : '展开';
        });

        const copyBtn = head.querySelector('.md-code-copy');
        if (copyBtn) head.insertBefore(btn, copyBtn);
        else head.appendChild(btn);
      });
    },

    _scrollToBottom() {
      const body = this.$refs.body;
      if (!body) return;
      body.scrollTop = body.scrollHeight;
    },
    _onScroll() {
      const body = this.$refs.body;
      if (!body) return;
      const distance = body.scrollHeight - body.scrollTop - body.clientHeight;
      this.stickToBottom = distance < 24;
      if (this.stickToBottom) this.unreadLines = 0;
    },
    scrollToBottom() {
      this.manualFollowPaused = false;
      this.stickToBottom = true;
      this.unreadLines = 0;
      this.$nextTick(() => this._scrollToBottom());
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
