/* Agent-log markdown parsing and code-block enhancement boundary. */
/* global CP */

window.CP = window.CP || {};

CP.AgentLogRenderBoundary = CP.AgentLogRenderBoundary || (() => {
  const SECTION_HEAD_RE = /^##\s+/;
  const LIVE_HEAD_RE = /^##\s+Live Output\s*$/i;
  const FENCE_RE = /^\s*(```+|~~~+)/;
  const ROLE_MARK_RE = /^(user|codex|claude|assistant)$/i;
  const COLLAPSE_LANG_RE = /\blanguage-(shell|bash|sh|powershell|ps1|cmd|zsh|console)\b/i;
  const DIFF_LANG_RE = /\blanguage-diff\b/i;
  const DIFF_COLLAPSE_MIN_LINES = 16;
  const DIFF_COLLAPSE_PREVIEW_ROWS = 10;
  const GIT_STATUS_PATH_RE = /^(\s*(?:\?\?|[ MADRCU]{1,2})\s+)(.+)$/;
  const EXEC_CMD_LINE_RE = /^(.+?)\s+in\s+((?:[A-Za-z]:[\\/]|\/).+)$/;
  const EXEC_OK_LINE_RE = /^\s*succeeded in \d+ms:/i;
  const EXEC_FAIL_LINE_RE = /^\s*(?:failed in \d+ms:|failed:|error:|fatal:)/i;
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

  function chunkSectionLines(lines, isLiveSection) {
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
  }

  function normalizeLiveOutputSection(lines) {
    if (!lines || !lines.length) return lines || [];
    const head = String(lines[0] || '').trim();
    if (!LIVE_HEAD_RE.test(head)) return lines;

    let bodyStart = 1;
    while (bodyStart < lines.length && !String(lines[bodyStart] || '').trim()) bodyStart += 1;
    if (bodyStart >= lines.length) return lines;

    const open = String(lines[bodyStart] || '').trim();
    const fenceOpen = FENCE_RE.exec(open);
    if (!fenceOpen) return lines;
    const fenceToken = fenceOpen[1];
    const fenceChar = fenceToken[0] || '`';

    let bodyEnd = -1;
    for (let i = bodyStart + 1; i < lines.length; i++) {
      const cur = String(lines[i] || '').trim();
      const m = FENCE_RE.exec(cur);
      if (!m) continue;
      if ((m[1][0] || '`') === fenceChar) {
        bodyEnd = i;
        break;
      }
    }
    const hasClosingFence = bodyEnd >= 0;
    if (!hasClosingFence) bodyEnd = lines.length;

    const inner = lines.slice(bodyStart + 1, bodyEnd);
    const hasCodexMarkers = inner.some((line) => {
      const trimmed = String(line || '').trim();
      return ROLE_MARK_RE.test(trimmed) || trimmed.toLowerCase() === 'exec';
    });
    if (!hasCodexMarkers) return lines;

    const out = [String(lines[0] || ''), ''];
    let mode = '';
    let runtimeOpen = false;
    let execOpen = false;

    const closeRuntime = () => {
      if (!runtimeOpen) return;
      out.push('~~~', '');
      runtimeOpen = false;
    };
    const closeExec = () => {
      if (!execOpen) return;
      out.push('~~~', '');
      execOpen = false;
    };
    const openRole = (name) => {
      closeRuntime();
      closeExec();
      out.push(`### ${name}`, '');
      mode = name.toLowerCase();
    };
    const openExec = () => {
      closeRuntime();
      closeExec();
      out.push('### Exec', '', '~~~text');
      execOpen = true;
      mode = 'exec';
    };
    const openRuntime = () => {
      if (mode === 'runtime' && runtimeOpen) return;
      closeExec();
      if (!runtimeOpen) out.push('### Runtime', '', '~~~text');
      runtimeOpen = true;
      mode = 'runtime';
    };

    for (let i = 0; i < inner.length; i++) {
      const line = String(inner[i] || '');
      const trimmed = line.trim();
      if (ROLE_MARK_RE.test(trimmed)) {
        openRole(trimmed[0].toUpperCase() + trimmed.slice(1).toLowerCase());
        continue;
      }
      if (trimmed.toLowerCase() === 'exec') {
        openExec();
        continue;
      }
      if (!mode) openRuntime();

      out.push(line);
    }

    closeRuntime();
    closeExec();

    const tail = hasClosingFence ? lines.slice(bodyEnd + 1) : [];
    if (tail.length) out.push(...tail);
    return out;
  }

  function parseMarkdownBlocks(raw, renderBlockMarkdown) {
    if (!raw) return [];
    const lines = raw.split(/\r?\n/);
    const sections = splitSections(lines);
    const out = [];

    for (let s = 0; s < sections.length; s++) {
      const [start, end] = sections[s];
      let secLines = lines.slice(start, end);
      const head = String(secLines[0] || '').trim();
      const isLive = LIVE_HEAD_RE.test(head);
      if (isLive) secLines = normalizeLiveOutputSection(secLines);
      const chunks = chunkSectionLines(secLines, isLive);

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
          section: isLive ? 'live' : 'other',
        });
      }
    }
    return out;
  }

  function decorateDiffPre(pre, source) {
    if (!(pre instanceof HTMLElement)) return;
    if (pre.dataset.alDiffDecorated === '1') return;
    const lines = String(source || '').split(/\r?\n/);
    const classify = (line) => {
      if (!line) return ['ctx', ''];
      if (EXEC_OK_LINE_RE.test(line)) return ['exec-ok', ''];
      if (EXEC_FAIL_LINE_RE.test(line)) return ['exec-fail', ''];
      if (EXEC_CMD_LINE_RE.test(line)) return ['exec-cmd', ''];
      if (/^diff --git\s+/.test(line)) return ['hdr', ''];
      if (/^(index |Binary files |similarity index |rename from |rename to |new file mode |deleted file mode )/.test(line)) return ['meta', ''];
      if (/^(---|\+\+\+)\s/.test(line)) return ['meta', ''];
      if (/^@@ .* @@/.test(line)) return ['hunk', ''];
      if (line.startsWith('+')) return ['add', '+'];
      if (line.startsWith('-')) return ['del', '-'];
      if (line.startsWith(' ')) return ['ctx', ' '];
      return ['ctx', ''];
    };
    const parseHunkStart = (line) => {
      const m = /^@@\s*-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s*@@/.exec(String(line || ''));
      if (!m) return null;
      return {
        oldStart: Number.parseInt(m[1], 10) || 0,
        newStart: Number.parseInt(m[2], 10) || 0,
      };
    };
    let oldLn = 0;
    let newLn = 0;
    let inHunk = false;
    const rendered = lines.map((line) => {
      const [kind, marker] = classify(line);
      const body = marker ? line.slice(1) : line;
      let oldLabel = '';
      let newLabel = '';
      let bodyHtml = _escapeHtml(body);
      if (kind === 'hdr') {
        const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
        if (m) {
          bodyHtml = `diff --git a/<span class="al-md-path">${_escapeHtml(m[1])}</span> b/<span class="al-md-path">${_escapeHtml(m[2])}</span>`;
        }
      } else if (kind === 'meta') {
        const m = /^(---|\+\+\+)\s+(?:[ab]\/)?(.+)$/.exec(line);
        if (m) {
          bodyHtml = `${_escapeHtml(m[1])} <span class="al-md-path">${_escapeHtml(m[2])}</span>`;
        }
      } else if (kind === 'hunk') {
        const start = parseHunkStart(line);
        if (start) {
          oldLn = start.oldStart;
          newLn = start.newStart;
          inHunk = true;
        }
      } else if (kind === 'exec-cmd') {
        const cm = EXEC_CMD_LINE_RE.exec(line);
        if (cm) {
          const cmdPart = String(cm[1] || '');
          const cwdPart = String(cm[2] || '');
          const split = /^(\s*)("[^"]+"|\S+)(.*)$/.exec(cmdPart);
          if (split) {
            bodyHtml = (
              `${_escapeHtml(split[1] || '')}` +
              `<span class="al-md-exec-bin">${_escapeHtml(split[2] || '')}</span>` +
              `<span class="al-md-exec-args">${_escapeHtml(split[3] || '')}</span>` +
              `<span class="al-md-exec-in"> in </span>` +
              `<span class="al-md-path">${_escapeHtml(cwdPart)}</span>`
            );
          } else {
            bodyHtml = `${_escapeHtml(cmdPart)}<span class="al-md-exec-in"> in </span><span class="al-md-path">${_escapeHtml(cwdPart)}</span>`;
          }
        }
      } else if (inHunk) {
        if (kind === 'add') {
          newLabel = String(newLn || '');
          newLn += 1;
        } else if (kind === 'del') {
          oldLabel = String(oldLn || '');
          oldLn += 1;
        } else if (kind === 'ctx') {
          oldLabel = String(oldLn || '');
          newLabel = String(newLn || '');
          oldLn += 1;
          newLn += 1;
        }
      }
      return (
        `<span class="al-md-diff-line k-${kind}" data-marker="${_escapeHtml(marker)}">` +
        `<span class="al-md-diff-gutter">` +
        `<span class="al-md-ln old">${_escapeHtml(oldLabel)}</span>` +
        `<span class="al-md-ln new">${_escapeHtml(newLabel)}</span>` +
        `<span class="al-md-sign">${_escapeHtml(marker)}</span>` +
        `</span>` +
        `<span class="al-md-diff-text">${bodyHtml}</span>` +
        `</span>`
      );
    }).join('');

    pre.classList.add('al-md-diff-pre');
    pre.innerHTML = `<code class="al-md-diff-code">${rendered}</code>`;
    pre.dataset.alDiffDecorated = '1';
  }

  function decorateExecPre(pre, source) {
    if (!(pre instanceof HTMLElement)) return;
    if (pre.dataset.alExecDecorated === '1' || pre.dataset.alDiffDecorated === '1') return;
    const lines = String(source || '').split(/\r?\n/);
    const hasExecSignal = lines.some((line) => EXEC_CMD_LINE_RE.test(line) || EXEC_OK_LINE_RE.test(line) || EXEC_FAIL_LINE_RE.test(line));
    const hasPathSignal = lines.some((line) => !!GIT_STATUS_PATH_RE.exec(line));
    if (!hasExecSignal && !hasPathSignal) return;

    const renderPathSegment = (text) => {
      const raw = String(text || '');
      if (!raw) return '';
      const renamed = raw.split(/\s+->\s+/);
      if (renamed.length > 1) {
        return renamed.map((seg) => `<span class="al-md-path">${_escapeHtml(seg)}</span>`).join('<span class="al-md-path-arrow"> -> </span>');
      }
      return `<span class="al-md-path">${_escapeHtml(raw)}</span>`;
    };

    const rendered = lines.map((line) => {
      if (EXEC_OK_LINE_RE.test(line)) {
        return `<span class="al-md-exec-line k-ok">${_escapeHtml(line)}</span>`;
      }
      if (EXEC_FAIL_LINE_RE.test(line)) {
        return `<span class="al-md-exec-line k-fail">${_escapeHtml(line)}</span>`;
      }
      const cmdM = EXEC_CMD_LINE_RE.exec(line);
      if (cmdM) {
        const cmdPart = String(cmdM[1] || '');
        const cwdPart = String(cmdM[2] || '');
        const split = /^(\s*)("[^"]+"|\S+)(.*)$/.exec(cmdPart);
        if (split) {
          return (
            `<span class="al-md-exec-line k-cmd">` +
            `${_escapeHtml(split[1] || '')}` +
            `<span class="al-md-exec-bin">${_escapeHtml(split[2] || '')}</span>` +
            `<span class="al-md-exec-args">${_escapeHtml(split[3] || '')}</span>` +
            `<span class="al-md-exec-in"> in </span>` +
            `<span class="al-md-path">${_escapeHtml(cwdPart)}</span>` +
            `</span>`
          );
        }
        return (
          `<span class="al-md-exec-line k-cmd">` +
          `${_escapeHtml(cmdPart)}<span class="al-md-exec-in"> in </span><span class="al-md-path">${_escapeHtml(cwdPart)}</span>` +
          `</span>`
        );
      }
      const m = GIT_STATUS_PATH_RE.exec(line);
      if (!m) return `<span class="al-md-exec-line k-out">${_escapeHtml(line)}</span>`;
      const prefix = String(m[1] || '');
      const path = String(m[2] || '');
      return (
        `<span class="al-md-exec-line k-path">` +
        `<span class="al-md-path-prefix">${_escapeHtml(prefix)}</span>` +
        `${renderPathSegment(path)}` +
        `</span>`
      );
    }).join('');

    pre.classList.add('al-md-exec-pre');
    pre.innerHTML = `<code class="al-md-exec-code">${rendered}</code>`;
    pre.dataset.alExecDecorated = '1';
  }

  function enhanceCodeBlocks(root) {
    if (!root) return;
    const cards = root.querySelectorAll('.al-md-block .md-code');
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
      if (isDiff) decorateDiffPre(pre, source);
      else decorateExecPre(pre, source);
      const isCommand = COLLAPSE_LANG_RE.test(klass);
      const collapseLine = isCommand && lines.length <= 3 && maxLen > 180;
      const collapseDiff = isDiff && lines.length >= DIFF_COLLAPSE_MIN_LINES;
      const collapseLarge = !isDiff && lines.length > 34;
      if (!collapseLine && !collapseDiff && !collapseLarge) return;

      const collapsedRows = collapseLine ? 1 : (isDiff ? DIFF_COLLAPSE_PREVIEW_ROWS : 14);
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
      btn.textContent = hiddenRows > 0
        ? (isDiff ? `展开 Diff ${hiddenRows} 行` : `展开 ${hiddenRows} 行`)
        : '展开';
      btn.addEventListener('click', () => {
        const expanded = card.classList.toggle('is-expanded');
        card.classList.toggle('is-collapsed', !expanded);
        if (expanded) btn.textContent = '收起';
        else {
          btn.textContent = hiddenRows > 0
            ? (isDiff ? `展开 Diff ${hiddenRows} 行` : `展开 ${hiddenRows} 行`)
            : '展开';
        }
      });

      const copyBtn = head.querySelector('.md-code-copy');
      if (copyBtn) head.insertBefore(btn, copyBtn);
      else head.appendChild(btn);
    });
  }

  function scheduleEnhance(vm, onEnhance) {
    if (!vm || typeof onEnhance !== 'function') return;
    if (vm._enhanceRaf) return;
    vm._enhanceRaf = requestAnimationFrame(() => {
      vm._enhanceRaf = 0;
      onEnhance();
    });
  }

  return Object.freeze({
    createMarkdownCache,
    renderMarkdown,
    parseMarkdownBlocks,
    enhanceCodeBlocks,
    scheduleEnhance,
  });
})();
