/* <cp-agent-log :text="s.taskLog.text" :done="s.taskLog.done" tall follow>
 *
 * Block-level renderer for sub-agent CLI output:
 *   - Tool invocations render as compact cards.
 *   - Diff hunks render with filename strips and + / - row tint.
 *   - Narrative lines render as plain monospace text.
 *
 * This component now uses explicit virtual scrolling (viewport window +
 * overscan + top/bottom spacers) to keep DOM size small on long logs.
 */
/* global Vue, CP */

const PREFIX_RULES = [
  { cls: 'status-ok',   re: /^\s*[✓✔]\s/ },
  { cls: 'status-err',  re: /^\s*[✗✖✘×]\s/ },
  { cls: 'status-warn', re: /^\s*[⚠⚡]\s/ },
  { cls: 'prompt',      re: /^\s*(?:&gt;|»|▸)\s/ },
];

const TOOL_HEAD_RE = /^(\s*)([⏺●◉▲▸▶])\s+([A-Za-z_][\w.-]*)(?:\(([^)]*)\))?\s*(.*)$/;
const TOOL_BODY_RE = /^(\s*)[⎿└╰├│╭╮╯]\s?/;
const DIFF_HEAD_RE = /^diff --git a\/(.+?) b\/(.+)$/;
const DIFF_META_RE = /^(---|\+\+\+) [ab]\/(.+)$/;
const HUNK_RE = /^@@ .+ @@/;
const INDEX_LINE_RE = /^(index |Binary files |similarity index |rename from |rename to |new file mode |deleted file mode )/;

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
      collapsed: Object.create(null),
      vStart: 0,
      vEnd: 0,
      topPad: 0,
      bottomPad: 0,
    };
  },
  computed: {
    blocks() {
      return this._parse(this.text || '');
    },
    blockCount() { return this.blocks.length; },
    textLength() { return (this.text || '').length; },
    totalLines() {
      let n = 0;
      for (const b of this.blocks) n += b.lines ? b.lines.length : 1;
      return n;
    },
    visibleBlocks() {
      if (!this.blocks.length) return [];
      if (this.vEnd <= this.vStart) return this.blocks;
      return this.blocks.slice(this.vStart, this.vEnd);
    },
  },
  watch: {
    textLength() {
      this.$nextTick(() => {
        if (this.follow && this.stickToBottom) this._scrollToBottom();
        this._scheduleVirtualCalc();
      });
    },
    blockCount() {
      this.$nextTick(() => this._scheduleVirtualCalc());
    },
  },
  created() {
    this._heightCache = new Map();
    this._virtualRaf = 0;
    this._measureRaf = 0;
    this._lastClientHeight = 0;
  },
  mounted() {
    const body = this.$refs.body;
    if (!body) return;
    body.addEventListener('scroll', this._onScroll, { passive: true });
    if (typeof ResizeObserver !== 'undefined') {
      this._resizeObserver = new ResizeObserver(() => {
        const h = body.clientHeight || 0;
        if (h !== this._lastClientHeight) {
          this._lastClientHeight = h;
          this._scheduleVirtualCalc();
        }
      });
      this._resizeObserver.observe(body);
    }
    this.$nextTick(() => {
      this._scrollToBottom();
      this._scheduleVirtualCalc();
    });
  },
  beforeUnmount() {
    const body = this.$refs.body;
    if (body) body.removeEventListener('scroll', this._onScroll);
    if (this._resizeObserver) this._resizeObserver.disconnect();
    if (this._virtualRaf) cancelAnimationFrame(this._virtualRaf);
    if (this._measureRaf) cancelAnimationFrame(this._measureRaf);
  },
  methods: {
    _parse(raw) {
      if (!raw) return [];
      const lines = raw.split(/\r?\n/);
      const ansi = CP.getAnsiRenderer && CP.getAnsiRenderer();
      const hasAnsiGlobal = CP._ANSI_RE.test(raw);
      const blocks = [];
      let cur = null;

      const fresh = (type, extra, lineNo) => {
        const key = `${type}:${lineNo}`;
        cur = Object.assign({ type, key, lines: [] }, extra || {});
        blocks.push(cur);
        return cur;
      };
      const renderInline = (line) => {
        if (hasAnsiGlobal && ansi && CP._ANSI_RE.test(line)) return ansi.ansi_to_html(line);
        return CP.escapeHtml(line);
      };

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        const dHead = DIFF_HEAD_RE.exec(line);
        if (dHead) {
          fresh('diff', { file: dHead[2] || dHead[1], lines: [{ kind: 'hdr', text: line }] }, i);
          continue;
        }
        if (cur && cur.type === 'diff') {
          if (HUNK_RE.test(line)) {
            cur.lines.push({ kind: 'hunk', text: line });
            continue;
          }
          const metaM = DIFF_META_RE.exec(line);
          if (metaM) {
            if (metaM[1] === '+++') cur.file = metaM[2] || cur.file;
            cur.lines.push({ kind: 'meta', text: line });
            continue;
          }
          if (INDEX_LINE_RE.test(line)) {
            cur.lines.push({ kind: 'meta', text: line });
            continue;
          }
          if (line.startsWith('+')) { cur.lines.push({ kind: 'add', text: line }); continue; }
          if (line.startsWith('-')) { cur.lines.push({ kind: 'del', text: line }); continue; }
          if (line.startsWith(' ')) { cur.lines.push({ kind: 'ctx', text: line }); continue; }
          cur = null;
        }

        const tHead = TOOL_HEAD_RE.exec(line);
        if (tHead) {
          fresh('tool', {
            head: tHead[2],
            name: tHead[3] || 'tool',
            arg: tHead[4] || '',
            trail: tHead[5] || '',
            body: [],
          }, i);
          continue;
        }
        if (cur && cur.type === 'tool') {
          if (TOOL_BODY_RE.test(line) || /^\s{2,}\S/.test(line) || line === '') {
            const bodyLine = line.replace(TOOL_BODY_RE, '');
            cur.body.push({ text: bodyLine, html: renderInline(bodyLine) });
            continue;
          }
          cur = null;
        }

        if (!cur || cur.type !== 'text') fresh('text', { lines: [] }, i);
        let cls = 'plain';
        const escaped = CP.escapeHtml(line);
        for (const rule of PREFIX_RULES) {
          if (rule.re.test(escaped)) {
            cls = rule.cls;
            break;
          }
        }
        cur.lines.push({ cls, html: renderInline(line) || '&nbsp;' });
      }
      return blocks;
    },

    toggleCollapse(key) {
      this.collapsed[key] = !this.collapsed[key];
      this._heightCache.delete(key);
      this.$nextTick(() => this._scheduleVirtualCalc());
    },
    isCollapsed(block) {
      if (block.key in this.collapsed) return this.collapsed[block.key];
      if (block.type === 'tool' && block.body && block.body.length > 12) return true;
      return false;
    },
    visibleBody(block) {
      if (!block.body) return [];
      return this.isCollapsed(block) ? block.body.slice(0, 2) : block.body;
    },

    _estimateBlockHeight(block) {
      if (!block) return 32;
      if (block.type === 'tool') {
        const full = block.body ? block.body.length : 0;
        const shown = this.isCollapsed(block) ? Math.min(2, full) + (full > 2 ? 1 : 0) : full;
        return 42 + (shown * 19) + 8;
      }
      if (block.type === 'diff') {
        const rows = block.lines ? block.lines.length : 1;
        return 30 + (rows * 18) + 8;
      }
      const rows = block.lines ? block.lines.length : 1;
      return Math.max(24, (rows * 18) + 6);
    },
    _blockHeight(block) {
      return this._heightCache.get(block.key) || this._estimateBlockHeight(block);
    },
    _scheduleVirtualCalc() {
      if (this._virtualRaf) return;
      this._virtualRaf = requestAnimationFrame(() => {
        this._virtualRaf = 0;
        this._recalcVirtualWindow();
      });
    },
    _scheduleMeasure() {
      if (this._measureRaf) return;
      this._measureRaf = requestAnimationFrame(() => {
        this._measureRaf = 0;
        this._measureVisibleHeights();
      });
    },
    _recalcVirtualWindow() {
      const body = this.$refs.body;
      const blocks = this.blocks;
      const total = blocks.length;
      if (!body || !total) {
        this.vStart = 0;
        this.vEnd = total;
        this.topPad = 0;
        this.bottomPad = 0;
        return;
      }

      const viewH = body.clientHeight || 0;
      const scrollTop = body.scrollTop || 0;
      const overscan = Math.max(400, viewH * 1.5);
      const minY = Math.max(0, scrollTop - overscan);
      const maxY = scrollTop + viewH + overscan;

      const prefix = new Array(total + 1);
      prefix[0] = 0;
      for (let i = 0; i < total; i++) {
        prefix[i + 1] = prefix[i] + this._blockHeight(blocks[i]) + 6;
      }

      let start = 0;
      while (start < total && prefix[start + 1] < minY) start += 1;
      let end = start;
      while (end < total && prefix[end] < maxY) end += 1;
      end = Math.min(total, Math.max(end, start + 1));

      this.vStart = start;
      this.vEnd = end;
      this.topPad = prefix[start];
      this.bottomPad = Math.max(0, prefix[total] - prefix[end]);
      this.$nextTick(() => this._scheduleMeasure());
    },
    _measureVisibleHeights() {
      const body = this.$refs.body;
      if (!body) return;
      const nodes = body.querySelectorAll('.al-virtual-item[data-bkey]');
      let changed = false;
      nodes.forEach((node) => {
        const key = node.dataset.bkey;
        if (!key) return;
        const h = Math.ceil(node.getBoundingClientRect().height) + 6;
        if (!Number.isFinite(h) || h <= 0) return;
        if (this._heightCache.get(key) !== h) {
          this._heightCache.set(key, h);
          changed = true;
        }
      });
      if (changed) this._scheduleVirtualCalc();
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
      this._scheduleVirtualCalc();
    },
    scrollToBottom() {
      this.stickToBottom = true;
      this.$nextTick(() => {
        this._scrollToBottom();
        this._scheduleVirtualCalc();
      });
    },
  },
  template: `
    <div class="agent-log" :class="[tall ? 'tall' : '', done ? '' : 'streaming']">
      <div class="agent-log-head">
        <span class="dots"><i></i><i></i><i></i></span>
        <span class="title">{{ title }}</span>
        <span class="spacer"></span>
        <span v-if="!done" class="streaming-pill">streaming…</span>
        <span class="lines">{{ totalLines }} 行</span>
      </div>
      <div class="agent-log-bodywrap">
        <div ref="body" class="agent-log-body">
          <div v-if="topPad > 0" class="al-spacer" :style="{ height: topPad + 'px' }"></div>

          <div v-for="b in visibleBlocks" :key="b.key" class="al-virtual-item" :data-bkey="b.key">
            <div v-if="b.type === 'tool'" class="al-block al-tool" :class="{ collapsed: isCollapsed(b) }">
              <div class="al-tool-head" @click="toggleCollapse(b.key)">
                <span class="al-tool-glyph">{{ b.head }}</span>
                <span class="al-tool-name">{{ b.name }}</span>
                <span v-if="b.arg" class="al-tool-arg">({{ b.arg }})</span>
                <span v-if="b.trail" class="al-tool-trail">{{ b.trail }}</span>
                <span class="al-tool-caret" v-if="b.body && b.body.length">
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                    :style="{transform: isCollapsed(b) ? 'rotate(-90deg)' : 'rotate(0deg)'}">
                    <polyline points="6 9 12 15 18 9"/>
                  </svg>
                </span>
              </div>
              <div v-if="b.body && b.body.length" class="al-tool-body">
                <span v-for="(row, i) in visibleBody(b)" :key="b.key + ':body:' + i" class="al-line" v-html="row.html || '&nbsp;'"></span>
                <button v-if="isCollapsed(b) && b.body.length > 2" class="al-expand" @click="toggleCollapse(b.key)">
                  … 展开 {{ b.body.length - 2 }} 行
                </button>
              </div>
            </div>

            <div v-else-if="b.type === 'diff'" class="al-block al-diff">
              <div class="al-diff-head">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <span class="al-diff-file">{{ b.file }}</span>
              </div>
              <div class="al-diff-body">
                <span v-for="(row, i) in b.lines" :key="b.key + ':diff:' + i" class="al-diff-line" :class="'k-' + row.kind">{{ row.text }}</span>
              </div>
            </div>

            <div v-else class="al-block al-text">
              <span v-for="(row, i) in b.lines" :key="b.key + ':text:' + i" class="al-line" :class="row.cls" v-html="row.html || '&nbsp;'"></span>
            </div>
          </div>

          <div v-if="bottomPad > 0" class="al-spacer" :style="{ height: bottomPad + 'px' }"></div>
          <div v-if="!blocks.length" class="al-empty">还没有可显示的日志</div>
          <span v-if="!done" class="al-cursor"></span>
        </div>
        <button v-if="!stickToBottom" class="agent-log-jump" @click="scrollToBottom" title="回到最新">
          ↓ 回到最新
        </button>
      </div>
    </div>
  `,
});
