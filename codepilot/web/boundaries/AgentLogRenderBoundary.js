/* Agent-log text parsing boundary.
 * Runtime logs are untrusted subprocess output. Keep them as text blocks so
 * copied source, HTML snippets, or Vue templates cannot become live DOM. */
/* global CP */

window.CP = window.CP || {};

CP.AgentLogRenderBoundary = CP.AgentLogRenderBoundary || (() => {
  const SECTION_HEAD_RE = /^##\s+/;
  const FENCE_RE = /^\s*(```+|~~~+)/;
  const CACHE_MAX_ENTRIES = 260;
  const LONG_TEXT_COLLAPSE_LINES = 48;
  const EXEC_LINE_RE = /^exec$/;
  const SPEAKER_LINE_RE = /^(user|assistant|codex|claude|opencode|system)$/i;
  const COMMAND_STATUS_RE = /^\s*(succeeded|exited|failed|timed out)\b.*:?$/i;

  function _escapeHtml(text) {
    const esc = CP.escapeHtml || ((t) => String(t)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;'));
    return esc(text);
  }

  function createMarkdownCache() {
    return new Map();
  }

  function cacheGet(cache, raw) {
    if (!cache || !cache.has(raw)) return null;
    const html = cache.get(raw);
    cache.delete(raw);
    cache.set(raw, html);
    return html;
  }

  function cacheSet(cache, raw, html) {
    if (!cache) return;
    cache.set(raw, html);
    while (cache.size > CACHE_MAX_ENTRIES) {
      const first = cache.keys().next();
      if (first && !first.done) cache.delete(first.value);
      else break;
    }
  }

  function renderMarkdown(cache, raw) {
    const key = String(raw || '');
    const cached = cacheGet(cache, key);
    if (cached != null) return cached;
    const html = _escapeHtml(key).replace(/\n/g, '<br>');
    cacheSet(cache, key, html);
    return html;
  }

  function splitSections(lines) {
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
  }

  function chunkSectionLines(lines) {
    if (!lines.length) return [];
    if (lines.length <= 180) return [lines];

    const chunks = [];
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
      if (cur.length < 220) return false;
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
  }

  function isExecLine(line) {
    return EXEC_LINE_RE.test(String(line || '').trim());
  }

  function isSpeakerLine(line) {
    return SPEAKER_LINE_RE.test(String(line || '').trim());
  }

  function compactLine(text, limit = 160) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (value.length <= limit) return value;
    return `${value.slice(0, Math.max(0, limit - 1))}…`;
  }

  function summarizeCommandRun(lines) {
    const command = compactLine(lines[1] || '命令');
    const statusLine = lines.find((line) => COMMAND_STATUS_RE.test(String(line || '')));
    const status = statusLine ? compactLine(statusLine, 80).replace(/:$/, '') : '';
    let tone = 'neutral';
    if (/succeeded/i.test(status)) tone = 'ok';
    else if (/exited|failed|timed out/i.test(status)) tone = 'fail';
    return { command, status, tone };
  }

  function makeCommandRun(lines, start) {
    const raw = lines.join('\n').replace(/\s+$/g, '');
    const summary = summarizeCommandRun(lines);
    return {
      type: 'command-run',
      key: `cmdrun:${start}:${lines.length}:${raw.length}`,
      raw,
      lineCount: lines.length,
      command: summary.command,
      status: summary.status,
      tone: summary.tone,
      collapsed: true,
    };
  }

  function parseCommandRuns(lines) {
    const items = [];
    const pendingRuns = [];
    let textStart = 0;

    const flushRuns = () => {
      if (!pendingRuns.length) return;
      const raw = pendingRuns.map((run) => run.raw).join('\n\n');
      const start = Number(String(pendingRuns[0].key).split(':')[1]) || 0;
      const lineCount = pendingRuns.reduce((sum, run) => sum + run.lineCount, 0);
      items.push({
        type: 'command-group',
        key: `cmd:${start}:${pendingRuns.length}:${raw.length}`,
        raw,
        lineCount,
        commandCount: pendingRuns.length,
        runs: pendingRuns.splice(0),
        collapsed: true,
      });
    };

    const pushText = (start, end) => {
      if (end <= start) return;
      flushRuns();
      items.push({
        type: 'text',
        start,
        lines: lines.slice(start, end),
      });
    };

    let i = 0;
    while (i < lines.length) {
      if (!isExecLine(lines[i])) {
        i += 1;
        continue;
      }

      pushText(textStart, i);
      const runStart = i;
      i += 1;
      while (i < lines.length && !isExecLine(lines[i]) && !isSpeakerLine(lines[i])) {
        i += 1;
      }
      pendingRuns.push(makeCommandRun(lines.slice(runStart, i), runStart));
      textStart = i;
    }

    pushText(textStart, lines.length);
    flushRuns();
    return items;
  }

  function pushTextBlocks(out, lines, baseStart) {
    if (!lines.length) return;
    const sections = splitSections(lines);

    for (let s = 0; s < sections.length; s++) {
      const [start, end] = sections[s];
      const secLines = lines.slice(start, end);
      const chunks = chunkSectionLines(secLines);

      for (let i = 0; i < chunks.length; i++) {
        const chunkLines = chunks[i];
        const rawChunk = chunkLines.join('\n').replace(/\s+$/g, '');
        if (!rawChunk) continue;
        const lineStart = baseStart + start;
        const collapsed = chunkLines.length > LONG_TEXT_COLLAPSE_LINES;
        out.push({
          type: 'log',
          key: `log:${lineStart}:${i}:${rawChunk.length}`,
          raw: rawChunk,
          lineCount: chunkLines.length,
          title: collapsed ? `${chunkLines.length} 行输出` : '',
          collapsed,
        });
      }
    }
  }

  function parseMarkdownBlocks(raw) {
    if (!raw) return [];
    const lines = raw.split(/\r?\n/);
    const items = parseCommandRuns(lines);
    const out = [];

    for (const item of items) {
      if (item.type === 'command-group') out.push(item);
      else pushTextBlocks(out, item.lines, item.start);
    }
    return out;
  }

  return Object.freeze({
    createMarkdownCache,
    renderMarkdown,
    parseMarkdownBlocks,
  });
})();
