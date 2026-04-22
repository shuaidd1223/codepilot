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
  { cls: 'status-info', re: /^\s*[•●]\s/ },
  { cls: 'prompt',      re: /^\s*(?:>|»|▸)\s/ },
  { cls: 'heading',     re: /^\s*#{1,4}\s/ },
  { cls: 'list',        re: /^\s*(?:[-*•]|\d+\.)\s/ },
  { cls: 'fence',       re: /^\s*```/ },
];
const TOOL_HEAD_RE = /^(\s*)([⏺●◉▲▸▶])\s+([A-Za-z_][\w.-]*)(?:\(([^)]*)\))?\s*(.*)$/;
const TOOL_BODY_RE = /^(\s*)[⎿└╰├│╭╮╯]\s?/;
const DIFF_HEAD_RE = /^diff --git a\/(.+?) b\/(.+)$/;
const DIFF_META_RE = /^(---|\+\+\+) [ab]\/(.+)$/;
const HUNK_RE = /^@@ .+ @@/;
const INDEX_LINE_RE = /^(index |Binary files |similarity index |rename from |rename to |new file mode |deleted file mode )/;
const ALERT_WORD_RE = /\b(error|failed|exception|traceback|timeout|denied|fatal)\b/i;
const ROLE_LINE_RE = /^\s*(user|codex|claude|assistant)\s*$/i;
const EXEC_LINE_RE = /^\s*exec\s*$/i;
const EXEC_CMD_RE = /^\s*".+"\s+in\s+.+$/;
const EXEC_OK_RE = /^\s*succeeded in \d+ms:/i;
const EXEC_FAIL_RE = /^\s*(?:failed in \d+ms:|failed:|error:)/i;
const META_SEP_RE = /^\s*-{4,}\s*$/;
const META_KV_RE = /^(workdir|model|provider|approval|sandbox|reasoning effort|reasoning summaries|session id):/i;
const DEF_LINE_RE = /^\s*(?:[+-]\s*)?def\s+[A-Za-z_][\w]*\s*\(/;
const CP_MD_BEGIN_RE = /^@@CP:MD-BEGIN(?:\s+(.*))?$/;
const CP_MD_END_RE = /^@@CP:MD-END$/;
const MD_HINT_LINE_RE = /^\s*(?:#{1,6}\s|```|~{3,}|\*\*.+\*\*|__.+__|[-*+]\s+\S|\d+\.\s+\S|>\s+\S|\|.+\|\s*)$/;
const LIVE_OUTPUT_HEAD_RE = /^\s*##\s+Live Output\s*$/i;
const DIFF_AUTO_COLLAPSE_MIN_LINES = 36;
const DIFF_COLLAPSE_PREVIEW_HEAD = 3;
const DIFF_COLLAPSE_PREVIEW_TAIL = 2;
const DIFF_HUNK_COLLAPSIBLE_MIN_LINES = 8;
const DIFF_HUNK_AUTO_COLLAPSE_MIN_LINES = 28;
const VIRTUAL_MIN_BLOCKS = 180;

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
      filterMode: 'all',
      searchQuery: '',
      searchPos: -1,
      unreadLines: 0,
      unreadDiffs: 0,
      pluginPanelOpen: false,
      pluginPanelKey: '',
      expandedRows: Object.create(null),
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
    displayBlocks() {
      if (this.filterMode === 'tools') return this.blocks.filter(b => b.type === 'tool');
      if (this.filterMode === 'diffs') return this.blocks.filter(b => b.type === 'diff');
      if (this.filterMode === 'alerts') return this.blocks.filter(b => this.isAlertBlock(b));
      return this.blocks;
    },
    filterCounts() {
      const tools = this.blocks.filter(b => b.type === 'tool').length;
      const diffs = this.blocks.filter(b => b.type === 'diff').length;
      const alerts = this.blocks.filter(b => this.isAlertBlock(b)).length;
      return {
        all: this.blocks.length,
        tools,
        diffs,
        alerts,
      };
    },
    blockCount() { return this.displayBlocks.length; },
    textLength() { return (this.text || '').length; },
    followEnabled() {
      return !!this.follow && !this.manualFollowPaused;
    },
    diffBlocks() {
      return this.blocks.filter(b => b.type === 'diff');
    },
    totalDiffCount() {
      return this.diffBlocks.length;
    },
    collapsibleDiffBlocks() {
      return this.diffBlocks.filter(b => b && b.lines && b.lines.length > 8);
    },
    diffBlockCount() {
      return this.collapsibleDiffBlocks.length;
    },
    allDiffsCollapsed() {
      if (!this.collapsibleDiffBlocks.length) return false;
      return this.collapsibleDiffBlocks.every(b => this.isCollapsed(b));
    },
    totalLines() {
      let n = 0;
      for (const b of this.blocks) n += b.lineCount || (b.lines ? b.lines.length : 1);
      return n;
    },
    displayLines() {
      let n = 0;
      for (const b of this.displayBlocks) n += b.lineCount || (b.lines ? b.lines.length : 1);
      return n;
    },
    linesLabel() {
      if (this.filterMode === 'all') return `${this.totalLines} 行`;
      return `${this.displayLines}/${this.totalLines} 行`;
    },
    jumpLabel() {
      const pieces = [];
      if (this.unreadLines > 0) pieces.push(`+${this.unreadLines} 行`);
      if (this.unreadDiffs > 0) pieces.push(`+${this.unreadDiffs} Diff`);
      return pieces.length ? `↓ 回到最新 · ${pieces.join(' · ')}` : '↓ 回到最新';
    },
    filterOptions() {
      return [
        { mode: 'all', label: '全部' },
        { mode: 'tools', label: '工具' },
        { mode: 'diffs', label: 'Diff' },
        { mode: 'alerts', label: '关注' },
      ];
    },
    searchMatches() {
      const q = String(this.searchQuery || '').trim().toLowerCase();
      if (!q) return [];
      const out = [];
      for (let i = 0; i < this.displayBlocks.length; i++) {
        const text = this.blockSearchText(this.displayBlocks[i]);
        if (text && text.toLowerCase().includes(q)) out.push(i);
      }
      return out;
    },
    searchMatchSet() {
      return new Set(this.searchMatches);
    },
    activeSearchDisplayIndex() {
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
    pluginPanelBlock() {
      if (!this.pluginPanelKey) return null;
      return this.blocks.find(b => b && b.type === 'diff' && b.key === this.pluginPanelKey) || null;
    },
    shouldVirtualize() {
      if (this.displayBlocks.length <= VIRTUAL_MIN_BLOCKS) return false;
      return !this.displayBlocks.some(b => b && b.type === 'markdown');
    },
    visibleBlocks() {
      if (!this.displayBlocks.length) return [];
      if (!this.shouldVirtualize) return this.displayBlocks;
      if (this.vEnd <= this.vStart) return this.displayBlocks;
      return this.displayBlocks.slice(this.vStart, this.vEnd);
    },
  },
  watch: {
    textLength() {
      this.$nextTick(() => {
        if (!this.textLength) this._clearUnread();
        if (this.followEnabled && this.stickToBottom) this._scrollToBottom();
        this._scheduleVirtualCalc();
      });
    },
    totalLines(next, prev) {
      const oldVal = Number.isFinite(prev) ? prev : 0;
      const delta = Math.max(0, (Number.isFinite(next) ? next : 0) - oldVal);
      if (!delta) return;
      if (!this.followEnabled || !this.stickToBottom) this.unreadLines += delta;
    },
    totalDiffCount(next, prev) {
      const oldVal = Number.isFinite(prev) ? prev : 0;
      const delta = Math.max(0, (Number.isFinite(next) ? next : 0) - oldVal);
      if (!delta) return;
      if (!this.followEnabled || !this.stickToBottom) this.unreadDiffs += delta;
    },
    blockCount() {
      this.$nextTick(() => this._scheduleVirtualCalc());
    },
    filterMode() {
      this.vStart = 0;
      this.vEnd = 0;
      this.topPad = 0;
      this.bottomPad = 0;
      this.$nextTick(() => this._scheduleVirtualCalc());
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
    pluginPanelBlock(next) {
      if (!next && this.pluginPanelOpen) this.closePluginDiff();
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
    window.addEventListener('keydown', this._onWindowKeydown);
  },
  beforeUnmount() {
    const body = this.$refs.body;
    if (body) body.removeEventListener('scroll', this._onScroll);
    if (this._resizeObserver) this._resizeObserver.disconnect();
    if (this._virtualRaf) cancelAnimationFrame(this._virtualRaf);
    if (this._measureRaf) cancelAnimationFrame(this._measureRaf);
    window.removeEventListener('keydown', this._onWindowKeydown);
  },
  methods: {
    _parse(raw) {
      if (!raw) return [];
      const lines = raw.split(/\r?\n/);
      const hasMdSignal = !!(CP._MD_SIGNAL_RE && CP._MD_SIGNAL_RE.test(raw));
      const hasStructuredBlocks = lines.some(line => DIFF_HEAD_RE.test(line) || TOOL_HEAD_RE.test(line));
      if (hasMdSignal && !hasStructuredBlocks) {
        return [{
          type: 'markdown',
          key: 'markdown:0',
          raw,
          html: (CP.renderOutput ? CP.renderOutput(raw) : CP.escapeHtml(raw).replace(/\n/g, '<br>')),
          lineCount: lines.length || 1,
        }];
      }
      const ansi = CP.getAnsiRenderer && CP.getAnsiRenderer();
      const hasAnsiGlobal = CP._ANSI_RE.test(raw);
      const blocks = [];
      let cur = null;
      let roleContext = '';
      let channelContext = '';

      const fresh = (type, extra, lineNo) => {
        const key = `${type}:${lineNo}`;
        cur = Object.assign({ type, key, lines: [] }, extra || {});
        blocks.push(cur);
        return cur;
      };
      const pushMarkdownBlock = (rawText, lineNo, meta) => {
        const mdRaw = String(rawText || '');
        const mdLines = mdRaw ? mdRaw.split(/\r?\n/) : [''];
        blocks.push({
          type: 'markdown',
          key: `markdown:${lineNo}`,
          raw: mdRaw,
          html: (CP.renderOutput ? CP.renderOutput(mdRaw) : CP.escapeHtml(mdRaw).replace(/\n/g, '<br>')),
          lineCount: mdLines.length || 1,
          meta: meta || '',
        });
        cur = null;
      };
      const renderInline = (line) => {
        if (hasAnsiGlobal && ansi && CP._ANSI_RE.test(line)) return ansi.ansi_to_html(line);
        return CP.escapeHtml(line);
      };
      const classifyTextLine = (line) => {
        const rawLine = String(line || '');
        const trimmed = rawLine.trim();

        const roleM = ROLE_LINE_RE.exec(trimmed);
        if (roleM) {
          roleContext = String(roleM[1] || '').toLowerCase();
          channelContext = '';
          return `role-${roleContext}`;
        }
        if (EXEC_LINE_RE.test(trimmed)) {
          roleContext = '';
          channelContext = 'exec';
          return 'exec-head';
        }
        if (META_SEP_RE.test(trimmed)) return 'meta-sep';
        if (META_KV_RE.test(trimmed)) return 'meta-kv';
        if (!trimmed) return 'plain';

        if (channelContext === 'exec') {
          if (EXEC_CMD_RE.test(rawLine)) return 'exec-cmd';
          if (EXEC_OK_RE.test(trimmed)) return 'exec-ok';
          if (EXEC_FAIL_RE.test(trimmed)) return 'exec-fail';
          return 'exec-out';
        }

        if (DEF_LINE_RE.test(rawLine)) return 'code-def';
        if (roleContext === 'codex') return 'speaker-codex';
        if (roleContext === 'claude') return 'speaker-claude';
        if (roleContext === 'user') return 'speaker-user';
        if (roleContext === 'assistant') return 'speaker-assistant';

        for (const rule of PREFIX_RULES) {
          if (rule.re.test(rawLine)) return rule.cls;
        }
        return 'plain';
      };

      let startIndex = 0;
      const firstNonEmpty = lines.findIndex(line => String(line || '').trim());
      if (firstNonEmpty >= 0 && /^#\s+Task\s+#\d+/i.test(String(lines[firstNonEmpty] || ''))) {
        let liveIdx = -1;
        for (let k = firstNonEmpty; k < lines.length; k++) {
          if (LIVE_OUTPUT_HEAD_RE.test(String(lines[k] || '').trim())) {
            liveIdx = k;
            break;
          }
        }
        if (liveIdx >= firstNonEmpty) {
          pushMarkdownBlock(lines.slice(firstNonEmpty, liveIdx + 1).join('\n'), firstNonEmpty, 'run-header');
          startIndex = liveIdx + 1;
          while (startIndex < lines.length && !String(lines[startIndex] || '').trim()) startIndex += 1;
        }
      }

      for (let i = startIndex; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = String(line || '').trim();

        const mdBegin = CP_MD_BEGIN_RE.exec(trimmed);
        if (mdBegin) {
          const mdMeta = mdBegin[1] || '';
          const mdLines = [];
          let j = i + 1;
          while (j < lines.length && !CP_MD_END_RE.test(String(lines[j] || '').trim())) {
            mdLines.push(lines[j]);
            j += 1;
          }
          pushMarkdownBlock(mdLines.join('\n'), i, mdMeta);
          i = j;
          continue;
        }

        const dHead = DIFF_HEAD_RE.exec(line);
        if (dHead) {
          fresh('diff', {
            file: dHead[2] || dHead[1],
            lines: [{ kind: 'hdr', text: line }],
            adds: 0,
            dels: 0,
            hunks: 0,
          }, i);
          continue;
        }
        if (cur && cur.type === 'diff') {
          if (HUNK_RE.test(line)) {
            cur.lines.push({ kind: 'hunk', text: line });
            cur.hunks += 1;
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
          if (line.startsWith('+')) { cur.lines.push({ kind: 'add', text: line }); cur.adds += 1; continue; }
          if (line.startsWith('-')) { cur.lines.push({ kind: 'del', text: line }); cur.dels += 1; continue; }
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

        const isAiRole = roleContext === 'codex' || roleContext === 'claude' || roleContext === 'assistant';
        const aiNarrative = isAiRole
          && channelContext !== 'exec'
          && !ROLE_LINE_RE.test(trimmed)
          && !EXEC_LINE_RE.test(trimmed)
          && !DIFF_HEAD_RE.test(line)
          && !TOOL_HEAD_RE.test(line);
        if (aiNarrative) {
          const mdLines = [line];
          let j = i + 1;
          while (j < lines.length) {
            const probe = lines[j];
            const pTrim = String(probe || '').trim();
            if (ROLE_LINE_RE.test(pTrim)
              || EXEC_LINE_RE.test(pTrim)
              || DIFF_HEAD_RE.test(probe)
              || TOOL_HEAD_RE.test(probe)
              || CP_MD_BEGIN_RE.test(pTrim)
              || CP_MD_END_RE.test(pTrim)
              || LIVE_OUTPUT_HEAD_RE.test(pTrim)) {
              break;
            }
            mdLines.push(probe);
            j += 1;
          }
          pushMarkdownBlock(mdLines.join('\n'), i, `speaker:${roleContext}`);
          i = j - 1;
          continue;
        }

        const inferMarkdown = channelContext !== 'exec'
          && !roleContext
          && MD_HINT_LINE_RE.test(trimmed);
        if (inferMarkdown) {
          const mdLines = [line];
          let j = i + 1;
          while (j < lines.length) {
            const probe = lines[j];
            const pTrim = String(probe || '').trim();
            if (ROLE_LINE_RE.test(pTrim)
              || EXEC_LINE_RE.test(pTrim)
              || DIFF_HEAD_RE.test(probe)
              || TOOL_HEAD_RE.test(probe)
              || CP_MD_BEGIN_RE.test(pTrim)
              || CP_MD_END_RE.test(pTrim)
              || LIVE_OUTPUT_HEAD_RE.test(pTrim)) {
              break;
            }
            mdLines.push(probe);
            j += 1;
          }
          pushMarkdownBlock(mdLines.join('\n'), i, 'inferred');
          i = j - 1;
          continue;
        }

        if (!cur || cur.type !== 'text') fresh('text', { lines: [] }, i);
        const cls = classifyTextLine(line);
        const foldable = cls === 'exec-cmd' && String(line || '').length > 150;
        const alert = cls === 'status-err'
          || cls === 'status-warn'
          || cls === 'exec-fail'
          || ALERT_WORD_RE.test(String(line || ''));
        cur.lines.push({
          id: `${i}:${cur.lines.length}`,
          cls,
          foldable,
          alert,
          text: line,
          html: renderInline(line) || '&nbsp;',
        });
      }
      return blocks;
    },

    toggleFollow() {
      this.manualFollowPaused = !this.manualFollowPaused;
      if (!this.manualFollowPaused) this.scrollToBottom();
    },
    setFilter(mode) {
      if (this.filterMode === mode) return;
      this.filterMode = mode;
    },
    canOpenPluginDiff(block) {
      if (!block || block.type !== 'diff') return false;
      const rows = (block.lines && block.lines.length) || 0;
      return rows > 0 && rows <= 6000;
    },
    openPluginDiff(block) {
      if (!this.canOpenPluginDiff(block)) return;
      this.pluginPanelKey = block.key;
      this.pluginPanelOpen = true;
    },
    closePluginDiff() {
      this.pluginPanelOpen = false;
      this.pluginPanelKey = '';
    },
    _onWindowKeydown(ev) {
      if (!this.pluginPanelOpen) return;
      if (!ev || ev.key !== 'Escape') return;
      this.closePluginDiff();
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
      this.scrollToDisplayIndex(this.searchMatches[next]);
    },
    prevMatch() {
      const total = this.searchMatches.length;
      if (!total) return;
      const prev = (this.searchPos - 1 + total) % total;
      this.searchPos = prev;
      this.scrollToDisplayIndex(this.searchMatches[prev]);
    },
    scrollToDisplayIndex(displayIndex) {
      const body = this.$refs.body;
      if (!body || !Number.isFinite(displayIndex) || displayIndex < 0) return;
      const blocks = this.displayBlocks;
      if (!blocks.length) return;
      const idx = Math.min(displayIndex, blocks.length - 1);
      let y = 0;
      for (let i = 0; i < idx; i++) y += this._blockHeight(blocks[i]) + 6;
      this.manualFollowPaused = true;
      this.stickToBottom = false;
      body.scrollTop = Math.max(0, y - 18);
      this._scheduleVirtualCalc();
    },
    toggleAllDiffs() {
      const targetCollapsed = !this.allDiffsCollapsed;
      for (const block of this.collapsibleDiffBlocks) {
        this.collapsed[block.key] = targetCollapsed;
        this._heightCache.delete(block.key);
      }
      this.$nextTick(() => this._scheduleVirtualCalc());
    },
    toggleCollapse(key) {
      this.collapsed[key] = !this.collapsed[key];
      this._heightCache.delete(key);
      this.$nextTick(() => this._scheduleVirtualCalc());
    },
    canCollapseDiff(block) {
      return !!(block && block.type === 'diff' && block.lines && block.lines.length > 8);
    },
    isCollapsed(block) {
      if (block.key in this.collapsed) return this.collapsed[block.key];
      if (block.type === 'tool' && block.body && block.body.length > 12) return true;
      if (block.type === 'diff' && block.lines && block.lines.length >= DIFF_AUTO_COLLAPSE_MIN_LINES) return true;
      return false;
    },
    visibleBody(block) {
      if (!block.body) return [];
      return this.isCollapsed(block) ? block.body.slice(0, 2) : block.body;
    },
    _diffHunkBodySizes(block) {
      if (!block || !block.lines || !block.lines.length) return [];
      const sizes = [];
      let idx = -1;
      for (const row of block.lines) {
        if (!row) continue;
        if (row.kind === 'hunk') {
          idx += 1;
          sizes[idx] = 0;
          continue;
        }
        if (idx >= 0) sizes[idx] += 1;
      }
      return sizes;
    },
    _diffHunkKey(block, idx) {
      return `${(block && block.key) || 'diff'}:hunk:${idx}`;
    },
    _isHunkCollapsed(hKey, bodyLines) {
      if (hKey in this.collapsed) return this.collapsed[hKey];
      return bodyLines >= DIFF_HUNK_AUTO_COLLAPSE_MIN_LINES;
    },
    toggleHunkCollapse(hKey, blockKey) {
      if (!hKey) return;
      this.collapsed[hKey] = !this.collapsed[hKey];
      if (blockKey) this._heightCache.delete(blockKey);
      this.$nextTick(() => this._scheduleVirtualCalc());
    },
    visibleDiffLines(block) {
      const lines = (block && block.lines) || [];
      if (!lines.length) return lines;
      if (this.canCollapseDiff(block) && this.isCollapsed(block)) {
        const head = lines.slice(0, DIFF_COLLAPSE_PREVIEW_HEAD);
        const tail = lines.slice(-DIFF_COLLAPSE_PREVIEW_TAIL);
        const hidden = Math.max(0, lines.length - head.length - tail.length);
        if (!hidden) return lines;
        return head.concat([{ kind: 'fold', text: `… 已折叠 ${hidden} 行（点击标题展开）` }], tail);
      }

      const hunkSizes = this._diffHunkBodySizes(block);
      if (!hunkSizes.length) return lines;
      const out = [];
      let hIdx = -1;
      let skipHunkBody = false;
      for (const row of lines) {
        if (!row) continue;
        if (row.kind === 'hunk') {
          hIdx += 1;
          skipHunkBody = false;
          const bodyLines = hunkSizes[hIdx] || 0;
          const hunkKey = this._diffHunkKey(block, hIdx);
          const collapsible = bodyLines >= DIFF_HUNK_COLLAPSIBLE_MIN_LINES;
          const collapsed = collapsible && this._isHunkCollapsed(hunkKey, bodyLines);
          out.push(Object.assign({}, row, {
            _hunkKey: hunkKey,
            _hunkCollapsible: collapsible,
            _hunkCollapsed: collapsed,
            _blockKey: block.key,
          }));
          if (collapsed) {
            out.push({
              kind: 'fold',
              text: `… hunk 折叠 ${bodyLines} 行（点击 @@ 展开）`,
              _hunkFold: true,
              _hunkKey: hunkKey,
              _blockKey: block.key,
            });
            skipHunkBody = true;
          }
          continue;
        }
        if (skipHunkBody) continue;
        out.push(row);
      }
      return out;
    },
    diffMarker(row) {
      if (!row || !row.text || !row.text.length) return '';
      if (row.kind === 'add' || row.kind === 'del' || row.kind === 'ctx') return row.text[0];
      return '';
    },
    diffText(row) {
      if (!row || !row.text) return '';
      if ((row.kind === 'add' || row.kind === 'del' || row.kind === 'ctx') && row.text.length > 0) {
        return row.text.slice(1);
      }
      return row.text;
    },
    isAlertBlock(block) {
      if (!block) return false;
      if (block.type === 'text') {
        return !!(block.lines && block.lines.some(row => row && row.alert));
      }
      if (block.type === 'tool') {
        if (ALERT_WORD_RE.test(String(block.trail || ''))) return true;
        return !!(block.body && block.body.some(row => ALERT_WORD_RE.test(String((row && row.text) || ''))));
      }
      return false;
    },
    _clearUnread() {
      this.unreadLines = 0;
      this.unreadDiffs = 0;
    },
    blockSearchText(block) {
      if (!block) return '';
      if (block.type === 'tool') {
        const parts = [block.name || '', block.arg || '', block.trail || ''];
        if (block.body && block.body.length) {
          for (const row of block.body) parts.push((row && row.text) || '');
        }
        return parts.join('\n');
      }
      if (block.type === 'diff') {
        return (block.lines || []).map(row => (row && row.text) || '').join('\n');
      }
      if (block.type === 'text') {
        return (block.lines || []).map(row => (row && row.text) || '').join('\n');
      }
      if (block.type === 'markdown') {
        return block.raw || '';
      }
      return '';
    },
    isSearchHit(displayIndex) {
      return this.searchMatchSet.has(displayIndex);
    },
    isSearchActive(displayIndex) {
      return this.activeSearchDisplayIndex === displayIndex;
    },
    rowUiKey(block, rowIndex, row) {
      const rkey = row && row.id ? row.id : String(rowIndex);
      return `${(block && block.key) || 'row'}:${rkey}`;
    },
    isRowCollapsed(block, rowIndex, row) {
      if (!row || !row.foldable) return false;
      const key = this.rowUiKey(block, rowIndex, row);
      return !this.expandedRows[key];
    },
    toggleRowCollapsed(block, rowIndex, row) {
      if (!row || !row.foldable) return;
      const key = this.rowUiKey(block, rowIndex, row);
      this.expandedRows[key] = !this.expandedRows[key];
      this.$nextTick(() => this._scheduleVirtualCalc());
    },
    rowHtml(block, rowIndex, row) {
      if (!row) return '&nbsp;';
      if (!this.isRowCollapsed(block, rowIndex, row)) return row.html || '&nbsp;';
      const text = String(row.text || '');
      const short = text.length > 220 ? `${text.slice(0, 220)} ...` : text;
      return CP.escapeHtml(short) || '&nbsp;';
    },

    _estimateBlockHeight(block) {
      if (!block) return 32;
      if (block.type === 'tool') {
        const full = block.body ? block.body.length : 0;
        const shown = this.isCollapsed(block) ? Math.min(2, full) + (full > 2 ? 1 : 0) : full;
        return 42 + (shown * 19) + 8;
      }
      if (block.type === 'diff') {
        const rows = this.visibleDiffLines(block).length || 1;
        return 30 + (rows * 18) + 8;
      }
      if (block.type === 'markdown') {
        const rows = block.lineCount || 1;
        return Math.max(42, (rows * 20) + 12);
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
      const blocks = this.displayBlocks;
      const total = blocks.length;
      if (!body || !total) {
        this.vStart = 0;
        this.vEnd = total;
        this.topPad = 0;
        this.bottomPad = 0;
        return;
      }
      if (!this.shouldVirtualize) {
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
      if (this.stickToBottom) this._clearUnread();
      this._scheduleVirtualCalc();
    },
    scrollToBottom() {
      this.manualFollowPaused = false;
      this.stickToBottom = true;
      this._clearUnread();
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
        <div class="al-filter-row">
          <button
            v-for="opt in filterOptions"
            :key="'flt:' + opt.mode"
            class="al-filter-btn"
            :class="{ active: filterMode === opt.mode }"
            @click="setFilter(opt.mode)">
            {{ opt.label }} <span class="n">{{ filterCounts[opt.mode] || 0 }}</span>
          </button>
        </div>
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
        <button
          v-if="diffBlockCount"
          class="al-head-btn"
          @click="toggleAllDiffs"
          :title="allDiffsCollapsed ? '展开所有 Diff 区块' : '折叠所有 Diff 区块'">
          {{ allDiffsCollapsed ? '展开 Diff' : '折叠 Diff' }}
        </button>
        <span v-if="!done" class="streaming-pill">streaming…</span>
        <span class="lines">{{ linesLabel }}</span>
      </div>
      <div class="agent-log-bodywrap">
        <div ref="body" class="agent-log-body">
          <div v-if="shouldVirtualize && topPad > 0" class="al-spacer" :style="{ height: topPad + 'px' }"></div>

            <div
              v-for="(b, vi) in visibleBlocks"
              :key="b.key"
              class="al-virtual-item"
              :class="{ 'is-hit': isSearchHit((shouldVirtualize ? vStart : 0) + vi), 'is-active': isSearchActive((shouldVirtualize ? vStart : 0) + vi) }"
              :data-bkey="b.key">
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

            <div v-else-if="b.type === 'diff'" class="al-block al-diff" :class="{ collapsed: isCollapsed(b) }">
              <div class="al-diff-head" :class="{ clickable: canCollapseDiff(b) }" @click="canCollapseDiff(b) && toggleCollapse(b.key)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <span class="al-diff-file">{{ b.file }}</span>
                <span class="al-diff-meta">+{{ b.adds || 0 }} / -{{ b.dels || 0 }}</span>
                <span class="al-diff-count">{{ (b.lines && b.lines.length) || 0 }} 行</span>
                <button
                  v-if="canOpenPluginDiff(b)"
                  class="al-diff-open"
                  @click.stop="openPluginDiff(b)"
                  title="在插件面板中查看这个 Diff">
                  插件查看
                </button>
                <span v-if="canCollapseDiff(b)" class="al-diff-caret">
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                    :style="{transform: isCollapsed(b) ? 'rotate(-90deg)' : 'rotate(0deg)'}">
                    <polyline points="6 9 12 15 18 9"/>
                  </svg>
                </span>
              </div>
              <div class="al-diff-body">
                <span
                  v-for="(row, i) in visibleDiffLines(b)"
                  :key="b.key + ':diff:' + i"
                  class="al-diff-line"
                  :class="[
                    'k-' + row.kind,
                    row._hunkCollapsible ? 'al-diff-hunk-toggle' : '',
                    row._hunkCollapsed ? 'al-diff-hunk-collapsed' : '',
                    row._hunkFold ? 'al-diff-hunk-fold' : ''
                  ]"
                  :title="row._hunkCollapsible ? (row._hunkCollapsed ? '展开 hunk' : '折叠 hunk') : ''"
                  @click="row._hunkKey && toggleHunkCollapse(row._hunkKey, b.key)"
                  :data-marker="diffMarker(row)">{{ diffText(row) }}</span>
              </div>
            </div>

            <div v-else-if="b.type === 'markdown'" class="al-block al-text al-text-md">
              <div class="md" v-html="b.html"></div>
            </div>

            <div v-else class="al-block al-text">
              <span
                v-for="(row, i) in b.lines"
                :key="b.key + ':text:' + i"
                class="al-line"
                :class="[row.cls, row.foldable ? 'al-line-foldable' : '', isRowCollapsed(b, i, row) ? 'is-collapsed' : '']"
                :title="row.foldable ? (isRowCollapsed(b, i, row) ? '点击展开完整命令' : '点击收起命令') : ''"
                @click="row.foldable && toggleRowCollapsed(b, i, row)">
                <span class="al-line-main" v-html="rowHtml(b, i, row)"></span>
                <span v-if="row.foldable" class="al-line-fold-hint">{{ isRowCollapsed(b, i, row) ? '展开' : '收起' }}</span>
              </span>
            </div>
          </div>

          <div v-if="shouldVirtualize && bottomPad > 0" class="al-spacer" :style="{ height: bottomPad + 'px' }"></div>
          <div v-if="!displayBlocks.length" class="al-empty">当前筛选下暂无日志</div>
          <span v-if="!done" class="al-cursor"></span>
        </div>
        <button v-if="!stickToBottom || manualFollowPaused" class="agent-log-jump" @click="scrollToBottom" title="回到最新并恢复自动跟随">
          {{ jumpLabel }}
        </button>
      </div>
      <div v-if="pluginPanelOpen" class="al-plugin-overlay" @click="closePluginDiff">
        <section class="al-plugin-panel" role="dialog" aria-modal="true" aria-label="Diff 插件查看器" @click.stop>
          <header class="al-plugin-head">
            <div class="al-plugin-title">
              <span>插件 Diff 查看</span>
              <code v-if="pluginPanelBlock">{{ pluginPanelBlock.file }}</code>
            </div>
            <button class="al-plugin-close" @click="closePluginDiff" title="关闭 (Esc)">关闭</button>
          </header>
          <div class="al-plugin-body">
            <cp-diff-viewer v-if="pluginPanelBlock" :block="pluginPanelBlock" :height="'100%'"></cp-diff-viewer>
            <div v-else class="al-empty">Diff 已失效，请重新选择</div>
          </div>
        </section>
      </div>
    </div>
  `,
});
