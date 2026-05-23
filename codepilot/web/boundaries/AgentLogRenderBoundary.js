/* Agent-log markdown parsing boundary — slim pass-through.
 * Parses raw Markdown text into renderable blocks.
 * No diff/exec decoration, no code-block collapsing, no legacy
 * Live Output normalization — marked.js + highlight.js handle
 * all syntax coloring from the subprocess's native output. */
/* global CP */

window.CP = window.CP || {};

CP.AgentLogRenderBoundary = CP.AgentLogRenderBoundary || (() => {
  const SECTION_HEAD_RE = /^##\s+/;
  const FENCE_RE = /^\s*(```+|~~~+)/;
  const CACHE_MAX_ENTRIES = 260;

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
    const html = (CP.renderOutput
      ? CP.renderOutput(key)
      : _escapeHtml(key).replace(/\n/g, '<br>'));
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

  function parseMarkdownBlocks(raw, renderBlockMarkdown) {
    if (!raw) return [];
    const lines = raw.split(/\r?\n/);
    const sections = splitSections(lines);
    const out = [];

    for (let s = 0; s < sections.length; s++) {
      const [start, end] = sections[s];
      const secLines = lines.slice(start, end);
      const chunks = chunkSectionLines(secLines);

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
          html: renderBlockMarkdown(rawChunk),
        });
      }
    }
    return out;
  }

  return Object.freeze({
    createMarkdownCache,
    renderMarkdown,
    parseMarkdownBlocks,
  });
})();
