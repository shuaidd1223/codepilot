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
  const SUPPORTING_TEXT_COLLAPSE_LINES = 8;
  const EXEC_LINE_RE = /^exec$/;
  const SPEAKER_LINE_RE = /^(user|assistant|system|tool|codex|claude|opencode)$/i;
  const COMMAND_STATUS_RE = /^\s*(succeeded|exited|failed|timed out)\b.*:?$/i;
  const LOG_META_RE = /^\s*CODEPILOT_LOG_META:\s*(\{.*\})\s*$/;
  const TELEMETRY_RE = /^\s*CODEPILOT_EXECUTOR_TELEMETRY:\s*(\{.*\})\s*$/;
  const JSON_FENCE_RE = /```(?:json)?\s*\n\s*(\{[\s\S]*?\})\s*\n\s*```/gi;
  const VERDICT_RE = /\bVERDICT\s*:\s*(PASS|FAIL)\b/i;
  const REVIEW_CONTEXT_RE = /review/i;
  const CRITICAL_TEXT_RE = /(\bVERDICT\s*:|###?\s*(Summary|Result)\b|已完成\s*#?\d*|验证结果|TDD evidence|需要修复|Full review comments|Traceback|AssertionError|^\s*(FAILED|ERROR)\b|\bException\b)/im;

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

  function executorMarkers(context = {}) {
    const markers = new Set(['codex', 'claude', 'opencode', 'codex-review', 'claude-review', 'opencode-review']);
    const add = (value) => {
      const text = String(value || '').trim().toLowerCase();
      if (!text) return;
      markers.add(text);
      const family = text.split(/[-_\s.]/)[0];
      if (family) {
        markers.add(family);
        markers.add(`${family}-review`);
        markers.add(`${family}-reviewer`);
      }
    };
    add(context.agent);
    add(context.executor);
    add(context.provider);
    add(context.phaseAgent);
    return markers;
  }

  function isSpeakerLine(line, context = {}) {
    const text = String(line || '').trim();
    if (!text) return false;
    if (SPEAKER_LINE_RE.test(text)) return true;
    return executorMarkers(context).has(text.toLowerCase());
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
      collapsed: summary.tone !== 'fail' || lines.length > 80,
    };
  }

  function isCommandNarrativeBoundary(lines, idx) {
    const line = String(lines[idx] || '').trim();
    if (line) return false;
    const next = String(lines[idx + 1] || '').trim();
    if (!next) return false;
    if (/^tokens used$/i.test(next)) return true;
    if (/^(已完成|实现要点|涉及的本任务文件|验证结果|注意|VERDICT\s*:)/i.test(next)) return true;
    return CRITICAL_TEXT_RE.test(next);
  }

  function parseCommandRuns(lines, context = {}, baseOffset = 0) {
    const items = [];
    const pendingRuns = [];
    let textStart = 0;

    const flushRuns = () => {
      if (!pendingRuns.length) return;
      const raw = pendingRuns.map((run) => run.raw).join('\n\n');
      const start = Number(String(pendingRuns[0].key).split(':')[1]) || baseOffset;
      const summedLineCount = pendingRuns.reduce((sum, run) => sum + (Number(run.lineCount) || 0), 0);
      const lineCount = summedLineCount || (raw ? raw.split(/\r?\n/).length : 0);
      const failedCount = pendingRuns.filter((run) => run.tone === 'fail').length;
      const succeededCount = pendingRuns.filter((run) => run.tone === 'ok').length;
      items.push({
        type: 'command-group',
        key: `cmd:${start}:${pendingRuns.length}:${raw.length}`,
        raw,
        lineCount,
        commandCount: pendingRuns.length,
        failedCount,
        succeededCount,
        runs: pendingRuns.splice(0),
        collapsed: failedCount === 0,
      });
    };

    const pushText = (start, end) => {
      if (end <= start) return;
      flushRuns();
      items.push({
        type: 'text',
        start: baseOffset + start,
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
      const localRunStart = i;
      const runStart = baseOffset + i;
      i += 1;
      while (i < lines.length && !isExecLine(lines[i]) && !isSpeakerLine(lines[i], context)) {
        if (isCommandNarrativeBoundary(lines, i)) break;
        i += 1;
      }
      pendingRuns.push(makeCommandRun(lines.slice(localRunStart, i), runStart));
      textStart = i;
    }

    pushText(textStart, lines.length);
    flushRuns();
    return items;
  }

  function leadingContextEnd(lines) {
    const liveOutput = lines.findIndex((line) => /^\s*##\s+Live Output\s*$/i.test(String(line || '')));
    if (liveOutput >= 0) {
      const head = lines.slice(0, liveOutput + 1).join('\n');
      if (/^\s*#\s+Task\s+#/m.test(head)) return liveOutput + 1;
    }
    const firstExec = lines.findIndex((line) => isExecLine(line));
    if (firstExec < 40) return -1;
    const head = lines.slice(0, firstExec).join('\n');
    if (!/^\s*#\s+Task\s+#/m.test(head)) return -1;
    if (!/(^\s*##\s+TDD Mode|\[Requirements\]|^\s*#\s+任务|^\s*##\s+Task Goal)/im.test(head)) return -1;
    return firstExec;
  }

  function textImportance(raw) {
    return CRITICAL_TEXT_RE.test(String(raw || '')) ? 'critical' : 'supporting';
  }

  function looksLikePromptContext(raw) {
    const text = String(raw || '');
    return /(```bash|```shell|\[Requirements\]|TDD Mode|Context And Planning|Implementation Rules|Hard Constraints|Quick Start|Standard Flow|Agent Development Rules)/i.test(text);
  }

  function parseTelemetryLine(line, start) {
    const match = TELEMETRY_RE.exec(String(line || ''));
    if (!match) return null;
    let payload = null;
    try {
      payload = JSON.parse(match[1]);
    } catch (_err) {
      payload = null;
    }
    const failed = (payload && payload.failed_executor && (payload.failed_executor.label || payload.failed_executor.family))
      || (payload && (payload.failed_executor_label || payload.failed_family))
      || '';
    const fallback = (payload && payload.fallback_executor && (payload.fallback_executor.label || payload.fallback_executor.family))
      || (payload && (payload.fallback_executor_label || payload.fallback_family))
      || '';
    const reason = compactLine((payload && payload.fallback_reason) || '', 80);
    const isFallback = payload && payload.kind === 'executor_fallback';
    const title = isFallback ? '执行器回退' : '执行器事件';
    const summaryParts = [];
    if (failed || fallback) summaryParts.push(`${failed || 'unknown'} → ${fallback || 'unknown'}`);
    if (reason) summaryParts.push(reason);
    const summary = summaryParts.length
      ? summaryParts.join(' · ')
      : compactLine((payload && (payload.summary || payload.message || payload.kind)) || line, 180);
    return {
      type: 'telemetry',
      key: `telemetry:${start}:${String(line || '').length}`,
      raw: String(line || '').trim(),
      title,
      summary,
      tone: isFallback ? 'warn' : 'neutral',
      payload: payload || {},
      collapsed: false,
      lineCount: 1,
    };
  }

  function parseLogMetaLine(line, start) {
    const match = LOG_META_RE.exec(String(line || ''));
    if (!match) return null;
    let payload = null;
    try {
      payload = JSON.parse(match[1]);
    } catch (_err) {
      payload = null;
    }
    if (!payload || typeof payload !== 'object') return null;
    const phase = compactLine(payload.phase || '', 40);
    const cwd = compactLine(payload.cwd || '', 70);
    const timeout = Number(payload.timeout_seconds || 0);
    const stdinChars = Number(payload.stdin_chars || 0);
    const redactionCount = Array.isArray(payload.command_redactions) ? payload.command_redactions.length : 0;
    const parts = [];
    if (phase) parts.push(phase);
    if (cwd) parts.push(cwd);
    if (Number.isFinite(timeout) && timeout > 0) parts.push(`timeout ${timeout}s`);
    if (redactionCount) parts.push(`命令参数已省略 ${redactionCount} 项`);
    if (Number.isFinite(stdinChars) && stdinChars > 0) parts.push(`stdin ${stdinChars} 字符`);
    const summary = parts.length ? parts.join(' · ') : 'live command';
    return {
      type: 'run-meta',
      key: `runmeta:${start}:${String(line || '').length}`,
      raw: `运行信息\n${summary}`,
      title: '运行信息',
      summary,
      payload,
      collapsed: false,
      lineCount: 1,
      importance: 'supporting',
    };
  }

  function extractLogMeta(lines) {
    const metaBlocks = [];
    const bodyLines = [];
    for (let i = 0; i < lines.length; i++) {
      const meta = parseLogMetaLine(lines[i], i);
      if (meta) {
        metaBlocks.push(meta);
        continue;
      }
      bodyLines.push(lines[i]);
    }
    return { metaBlocks, bodyLines };
  }

  function collectTelemetryBlocks(lines, baseStart) {
    const out = [];
    for (let i = 0; i < lines.length; i++) {
      const block = parseTelemetryLine(lines[i], baseStart + i);
      if (block) out.push(block);
    }
    return out;
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
        const importance = textImportance(rawChunk);
        const collapsed = importance !== 'critical' && (
          chunkLines.length > SUPPORTING_TEXT_COLLAPSE_LINES
          || chunkLines.length > LONG_TEXT_COLLAPSE_LINES
          || looksLikePromptContext(rawChunk)
        );
        out.push({
          type: 'log',
          key: `log:${lineStart}:${i}:${rawChunk.length}`,
          raw: rawChunk,
          lineCount: chunkLines.length,
          importance,
          title: collapsed ? `${chunkLines.length} 行输出` : '',
          collapsed,
        });
      }
    }
  }

  function pushTextAndTelemetryBlocks(out, lines, baseStart, context = {}) {
    let pending = [];
    let pendingStart = baseStart;

    const flush = () => {
      if (!pending.length) return;
      pushTextBlocks(out, pending, pendingStart);
      pending = [];
    };

    for (let i = 0; i < lines.length; i++) {
      const line = String(lines[i] || '');
      const telemetry = parseTelemetryLine(line, baseStart + i);
      if (telemetry) {
        flush();
        out.push(telemetry);
        pendingStart = baseStart + i + 1;
        continue;
      }
      if (isSpeakerLine(line, context) && parseTelemetryLine(lines[i + 1], baseStart + i + 1)) {
        flush();
        pendingStart = baseStart + i + 1;
        continue;
      }
      if (!pending.length) pendingStart = baseStart + i;
      pending.push(line);
    }
    flush();
  }

  function isReviewContext(context = {}) {
    return REVIEW_CONTEXT_RE.test(String(context.phase || ''))
      || REVIEW_CONTEXT_RE.test(String(context.kind || ''))
      || REVIEW_CONTEXT_RE.test(String(context.agent || ''));
  }

  function normalizeReviewBlock(review) {
    if (!review || typeof review !== 'object') return null;
    const verdict = String(review.verdict || '').trim().toLowerCase();
    if (!verdict) return null;
    const acRaw = Array.isArray(review.ac_checks) ? review.ac_checks : (Array.isArray(review.acChecks) ? review.acChecks : []);
    const listRaw = (name) => (Array.isArray(review[name]) ? review[name].map((item) => String(item || '').trim()).filter(Boolean) : []);
    return {
      verdict,
      source: String(review.source || '').trim(),
      acChecks: acRaw.map((item) => ({
        id: String((item && item.id) || '').trim(),
        status: String((item && item.status) || '').trim().toUpperCase(),
        reason: String((item && item.reason) || '').trim(),
      })).filter((item) => item.id || item.status || item.reason),
      blockers: listRaw('blockers'),
      advisory: listRaw('advisory'),
    };
  }

  function parseTrailingReviewJson(raw) {
    let parsed = null;
    const text = String(raw || '');
    JSON_FENCE_RE.lastIndex = 0;
    let match = JSON_FENCE_RE.exec(text);
    while (match) {
      try {
        const candidate = JSON.parse(match[1]);
        if (candidate && typeof candidate === 'object' && candidate.verdict) parsed = candidate;
      } catch (_err) {
        // Keep scanning older transcripts with malformed intermediate fences.
      }
      match = JSON_FENCE_RE.exec(text);
    }
    return normalizeReviewBlock(parsed);
  }

  function legacyReviewFindings(raw) {
    const lines = String(raw || '').split(/\r?\n/);
    const start = lines.findIndex((line) => /^(#+\s*)?(需要修复的点|需要修复|修复建议|Full review comments)\s*[:：]?\s*$/i.test(String(line || '').trim()));
    if (start < 0) return [];
    const out = [];
    for (let i = start + 1; i < lines.length; i++) {
      const line = String(lines[i] || '').trim();
      if (!line) {
        if (out.length) break;
        continue;
      }
      if (VERDICT_RE.test(line) || /^```/.test(line) || /^##\s+Result/i.test(line)) break;
      if (/^[-*]\s+/.test(line)) out.push(line.replace(/^[-*]\s+/, '').trim());
      else if (out.length) out[out.length - 1] = `${out[out.length - 1]} ${line}`;
      if (out.length >= 8) break;
    }
    return out.filter(Boolean);
  }

  function parseLegacyReview(raw) {
    const match = VERDICT_RE.exec(String(raw || ''));
    if (!match) return null;
    return {
      verdict: match[1].toLowerCase(),
      source: 'legacy',
      acChecks: [],
      blockers: legacyReviewFindings(raw),
      advisory: [],
    };
  }

  function makeReviewVerdictBlock(raw, context = {}) {
    const review = normalizeReviewBlock(context.review) || parseTrailingReviewJson(raw) || parseLegacyReview(raw);
    if (!review) return null;
    return {
      type: 'review-verdict',
      key: `review:${review.verdict}:${String(raw || '').length}`,
      raw: String(raw || ''),
      lineCount: String(raw || '').split(/\r?\n/).length,
      verdict: review.verdict,
      source: review.source,
      acChecks: review.acChecks,
      blockers: review.blockers,
      advisory: review.advisory,
      collapsed: false,
      importance: 'critical',
    };
  }

  function parseMarkdownBlocks(raw, context = {}) {
    if (!raw) return [];
    const extracted = extractLogMeta(raw.split(/\r?\n/));
    const lines = extracted.bodyLines;
    const metaBlocks = extracted.metaBlocks;
    const out = [];
    if (isReviewContext(context)) {
      const reviewBlock = makeReviewVerdictBlock(raw, context);
      if (reviewBlock) {
        out.push(reviewBlock);
        out.push(...metaBlocks);
        out.push(...collectTelemetryBlocks(lines, 0));
        const reviewRaw = lines.join('\n').replace(/\s+$/g, '');
        out.push({
          type: 'review-raw',
          key: `reviewraw:0:${reviewRaw.length}`,
          raw: reviewRaw,
          lineCount: lines.length,
          title: `Reviewer 原始输出 · ${lines.length} 行`,
          collapsed: true,
          importance: 'supporting',
        });
        return out;
      }
    }

    out.push(...metaBlocks);
    let bodyLines = lines;
    let baseOffset = 0;
    const contextEnd = leadingContextEnd(lines);
    if (contextEnd > 0) {
      const contextRaw = lines.slice(0, contextEnd).join('\n').replace(/\s+$/g, '');
      if (contextRaw) {
        out.push({
          type: 'log',
          key: `context:0:${contextEnd}:${contextRaw.length}`,
          raw: contextRaw,
          lineCount: contextEnd,
          importance: 'supporting',
          title: /^\s*##\s+Live Output\s*$/im.test(contextRaw) ? `运行器上下文 · ${contextEnd} 行` : `任务输入上下文 · ${contextEnd} 行`,
          collapsed: true,
        });
      }
      bodyLines = lines.slice(contextEnd);
      baseOffset = contextEnd;
    }

    const items = parseCommandRuns(bodyLines, context, baseOffset);

    for (const item of items) {
      if (item.type === 'command-group') out.push(item);
      else pushTextAndTelemetryBlocks(out, item.lines, item.start, context);
    }
    return out;
  }

  return Object.freeze({
    createMarkdownCache,
    renderMarkdown,
    parseMarkdownBlocks,
  });
})();
