/* <cp-agent-log :text="s.taskLog.text" :done="s.taskLog.done" tall follow>
 *
 * Block-level renderer for sub-agent CLI output. Modelled on the visual
 * language of the Claude Code VSCode extension and the Codex desktop app:
 *
 *   - Tool invocations render as compact *cards* with an accent rail, tool
 *     name, argument, and a collapsible body.
 *   - Diff hunks render with a filename strip and `+/-` rows that get the
 *     familiar green/red row tint.
 *   - Narrative / prose lines render as plain monospace text.
 *   - A blinking cursor tail trails the last line while `done=false`.
 *
 * Rendering is DOM-based: canvas would give us fast paint but lose text
 * selection, copy, a11y and hit-testing. Instead, each block is wrapped in
 * a container with `content-visibility: auto` so the browser natively
 * virtualises whatever scrolls out of the viewport (fast on 10k+ lines).
 */
/* global Vue, CP */

/* Line-prefix rules used when the surrounding block is "plain text". */
const PREFIX_RULES = [
  { cls: 'status-ok',   re: /^\s*[✓✔]\s/ },
  { cls: 'status-err',  re: /^\s*[✗✖✘×]\s/ },
  { cls: 'status-warn', re: /^\s*[⚠⚡]\s/ },
  { cls: 'prompt',      re: /^\s*(?:&gt;|»|▸)\s/ },
];

const TOOL_HEAD_RE   = /^(\s*)([⏺●◉▲▸▶])\s+([A-Za-z_][\w.-]*)(?:\(([^)]*)\))?\s*(.*)$/;
const TOOL_BODY_RE   = /^(\s*)[⎿└╰├│╭╮╯]\s?/;
const DIFF_HEAD_RE   = /^diff --git a\/(.+?) b\/(.+)$/;
const DIFF_META_RE   = /^(---|\+\+\+) [ab]\/(.+)$/;
const HUNK_RE        = /^@@ .+ @@/;
const INDEX_LINE_RE  = /^(index |Binary files |similarity index |rename from |rename to |new file mode |deleted file mode )/;

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
      /* Map of block.id → collapsed state, so tool outputs stay in the
       * same expanded/collapsed state across re-renders. */
      collapsed: Object.create(null),
    };
  },
  computed: {
    blocks() {
      return this._parse(this.text || '');
    },
    blockCount() { return this.blocks.length; },
    totalLines() {
      let n = 0;
      for (const b of this.blocks) n += b.lines ? b.lines.length : 1;
      return n;
    },
  },
  watch: {
    blockCount() {
      if (!this.follow || !this.stickToBottom) return;
      this.$nextTick(() => this._scrollToBottom());
    },
  },
  mounted() {
    const body = this.$refs.body;
    if (body) {
      body.addEventListener('scroll', this._onScroll, { passive: true });
      this._scrollToBottom();
    }
  },
  beforeUnmount() {
    const body = this.$refs.body;
    if (body) body.removeEventListener('scroll', this._onScroll);
  },
  methods: {
    /* ── Parsing ───────────────────────────────────────────
     * Walk the raw text line by line and emit blocks of these shapes:
     *   { type: 'tool',  id, head, arg, meta, body:[{line,cls}] }
     *   { type: 'diff',  id, file, lines:[{kind:'+','-',' ','meta','hdr','hunk', text}] }
     *   { type: 'text',  id, lines:[{cls, html}] }
     * Blocks are separated by blank-line runs or by the start of a new
     * structural marker (tool head / diff head). */
    _parse(raw) {
      const lines = raw.split(/\r?\n/);
      const ansi = CP.getAnsiRenderer && CP.getAnsiRenderer();
      const hasAnsiGlobal = CP._ANSI_RE.test(raw);
      const blocks = [];
      let cur = null;
      let diffFile = null;
      let blockSeq = 0;
      const fresh = (type, extra) => {
        cur = Object.assign({ type, id: ++blockSeq, lines: [] }, extra || {});
        blocks.push(cur);
        return cur;
      };
      const renderInline = (line) => {
        if (hasAnsiGlobal && ansi && CP._ANSI_RE.test(line)) return ansi.ansi_to_html(line);
        return CP.escapeHtml(line);
      };

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        /* --- Diff block start --- */
        const dHead = DIFF_HEAD_RE.exec(line);
        if (dHead) {
          diffFile = dHead[2] || dHead[1];
          fresh('diff', { file: diffFile, lines: [{ kind: 'hdr', text: line }] });
          continue;
        }
        if (cur && cur.type === 'diff') {
          if (HUNK_RE.test(line)) {
            cur.lines.push({ kind: 'hunk', text: line }); continue;
          }
          const metaM = DIFF_META_RE.exec(line);
          if (metaM) {
            /* keep track of the "after" filename for the strip */
            if (metaM[1] === '+++') cur.file = metaM[2] || cur.file;
            cur.lines.push({ kind: 'meta', text: line }); continue;
          }
          if (INDEX_LINE_RE.test(line)) {
            cur.lines.push({ kind: 'meta', text: line }); continue;
          }
          if (line.startsWith('+')) { cur.lines.push({ kind: 'add', text: line }); continue; }
          if (line.startsWith('-')) { cur.lines.push({ kind: 'del', text: line }); continue; }
          if (line.startsWith(' ')) { cur.lines.push({ kind: 'ctx', text: line }); continue; }
          /* A non-diff line ends the block. Fall through to generic handling. */
          cur = null;
          diffFile = null;
        }

        /* --- Tool call header --- */
        const tHead = TOOL_HEAD_RE.exec(line);
        if (tHead) {
          fresh('tool', {
            head: tHead[2],
            name: tHead[3] || 'tool',
            arg: tHead[4] || '',
            trail: tHead[5] || '',
            body: [],
          });
          continue;
        }
        /* --- Tool body continuation (⎿ / └ / indented) --- */
        if (cur && cur.type === 'tool') {
          if (TOOL_BODY_RE.test(line) || /^\s{2,}\S/.test(line) || line === '') {
            cur.body.push({ text: line.replace(TOOL_BODY_RE, ''), html: renderInline(line.replace(TOOL_BODY_RE, '')) });
            continue;
          }
          /* break out */
          cur = null;
        }

        /* --- Text block: accumulate lines --- */
        if (!cur || cur.type !== 'text') fresh('text', { lines: [] });
        let cls = 'plain';
        const escaped = CP.escapeHtml(line);
        for (const rule of PREFIX_RULES) {
          if (rule.re.test(escaped)) { cls = rule.cls; break; }
        }
        cur.lines.push({ cls, html: renderInline(line) || '&nbsp;' });
      }
      return blocks;
    },

    toggleCollapse(id) {
      this.collapsed[id] = !this.collapsed[id];
    },
    isCollapsed(block) {
      /* Long tool outputs default collapsed; user toggle overrides. */
      if (block.id in this.collapsed) return this.collapsed[block.id];
      if (block.type === 'tool' && block.body && block.body.length > 12) return true;
      return false;
    },
    visibleBody(block) {
      if (!block.body) return [];
      return this.isCollapsed(block) ? block.body.slice(0, 2) : block.body;
    },

    _scrollToBottom() {
      const body = this.$refs.body;
      if (body) body.scrollTop = body.scrollHeight;
    },
    _onScroll() {
      const body = this.$refs.body;
      if (!body) return;
      const distance = body.scrollHeight - body.scrollTop - body.clientHeight;
      this.stickToBottom = distance < 24;
    },
    scrollToBottom() {
      this.stickToBottom = true;
      this.$nextTick(() => this._scrollToBottom());
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
          <template v-for="b in blocks" :key="b.id">
            <!-- Tool call card -->
            <div v-if="b.type === 'tool'" class="al-block al-tool" :class="{ collapsed: isCollapsed(b) }">
              <div class="al-tool-head" @click="toggleCollapse(b.id)">
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
                <span v-for="(row, i) in visibleBody(b)" :key="i" class="al-line" v-html="row.html || '&nbsp;'"></span>
                <button v-if="isCollapsed(b) && b.body.length > 2" class="al-expand" @click="toggleCollapse(b.id)">
                  … 展开 {{ b.body.length - 2 }} 行
                </button>
              </div>
            </div>

            <!-- Diff block -->
            <div v-else-if="b.type === 'diff'" class="al-block al-diff">
              <div class="al-diff-head">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <span class="al-diff-file">{{ b.file }}</span>
              </div>
              <div class="al-diff-body">
                <span v-for="(row, i) in b.lines" :key="i" class="al-diff-line" :class="'k-' + row.kind">{{ row.text }}</span>
              </div>
            </div>

            <!-- Text block -->
            <div v-else class="al-block al-text">
              <span v-for="(row, i) in b.lines" :key="i" class="al-line" :class="row.cls" v-html="row.html || '&nbsp;'"></span>
            </div>
          </template>
          <span v-if="!done" class="al-cursor"></span>
        </div>
        <button v-if="!stickToBottom" class="agent-log-jump" @click="scrollToBottom" title="回到最新">
          ↓ 回到最新
        </button>
      </div>
    </div>
  `,
});
