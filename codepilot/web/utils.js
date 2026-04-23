/* CodePilot shared constants & helpers. Exposes window.CP. */
/* global Vue */

window.CP = window.CP || { Components: {} };

CP.STATUS_LABEL = {
  backlog: '待办', in_progress: '进行中', done: '已完成', failed: '失败',
  cancelled: '已取消', queued: '排队', running: '执行中', succeeded: '成功',
  attention: '需关注', error: '错误', info: '信息', warning: '警告',
  question: '问题', requirement: '需求', task: '任务', command: '命令',
};

CP.PHASE_LABEL = {
  queued: '排队中', planning: '规划中', running: '执行中',
  done: '完成', failed: '失败', attention: '需关注',
};

CP.TONE_MAP = {
  in_progress: 'info', running: 'info', planning: 'info', queued: 'neutral',
  backlog: 'neutral', done: 'success', succeeded: 'success',
  failed: 'danger', error: 'danger', cancelled: 'danger',
  warning: 'warning', attention: 'warning',
  info: 'info', question: 'primary', requirement: 'primary', task: 'primary',
  command: 'neutral',
};

CP.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

/* ANSI escape code renderer — lazy singleton. */
CP._ansi = null;
CP.getAnsiRenderer = () => {
  if (CP._ansi) return CP._ansi;
  if (typeof AnsiUp === 'undefined') return null;
  const a = new AnsiUp();
  a.use_classes = true;   /* so CSS — not inline styles — drives the palette */
  a.escape_html = true;
  CP._ansi = a;
  return a;
};

CP._ANSI_RE = /\u001b\[[0-9;?]*[A-Za-z]/;
CP._DIFF_RE = /(^|\n)(diff --git |--- |\+\+\+ |@@ |Index: )/;

/* Render log / code text to HTML with the best available highlighter.
 *   1. If the text carries ANSI escapes, pass through ansi_up (CLI palette).
 *   2. If it looks like a unified diff, force the `diff` grammar so +/- lines
 *      get the green/red tint users expect from Codex / Claude Code output.
 *   3. Otherwise fall back to highlight.js auto-detection for code snippets
 *      and plain-escape for prose.
 * `lang` is an optional hint (e.g. 'python', 'diff'). */
CP.renderCode = (text, lang) => {
  if (text == null || text === '') return '';
  const raw = String(text);
  if (CP._ANSI_RE.test(raw)) {
    const ansi = CP.getAnsiRenderer();
    if (ansi) return ansi.ansi_to_html(raw);
  }
  const hljs = window.hljs;
  const chosen = lang || (CP._DIFF_RE.test(raw) ? 'diff' : null);
  if (hljs) {
    try {
      if (chosen && hljs.getLanguage && hljs.getLanguage(chosen)) {
        return hljs.highlight(raw, { language: chosen, ignoreIllegals: true }).value;
      }
      if (chosen == null && raw.length < 20000) {
        /* Auto-detect only on smaller blobs — hljs auto-detect is O(n·langs). */
        return hljs.highlightAuto(raw).value;
      }
    } catch (e) { /* fall through to plain escape */ }
  }
  return CP.escapeHtml(raw);
};

/* Markdown renderer — GFM with hljs-highlighted code fences, sanitised via
 * DOMPurify. Configured once on first use; subsequent calls just feed the
 * cached `marked` instance. */
CP._markedReady = false;
CP._configureMarked = () => {
  if (CP._markedReady || typeof marked === 'undefined') return CP._markedReady;
  const renderer = new marked.Renderer();
  renderer.code = (code, lang) => {
    /* marked ≥ 9 passes a token object; older versions pass (text, lang). */
    const text = typeof code === 'string' ? code : (code && code.text) || '';
    const language = (typeof code === 'object' && code && code.lang) ? code.lang : lang;
    const body = CP.renderCode(text, language || null);
    const label = language ? CP.escapeHtml(language) : 'text';
    const langCls = language ? ` language-${CP.escapeHtml(language)}` : '';
    const source = CP.escapeHtml(text);
    /* Code block chrome: language tag + copy button. The copy button reads
     * the raw source from a data attribute, so clipboard copy always gets
     * the plain text (not hljs-highlighted HTML). */
    return (
      `<div class="md-code">` +
        `<div class="md-code-head">` +
          `<span class="md-code-lang">${label}</span>` +
          `<button class="md-code-copy" data-src="${source}" type="button" aria-label="复制">` +
            `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>` +
            `<span>复制</span>` +
          `</button>` +
        `</div>` +
        `<pre class="code hljs${langCls}"><code class="hljs${langCls}">${body}</code></pre>` +
      `</div>`
    );
  };
  /* Alerts: `> [!NOTE] ...` / [!WARNING] / [!TIP] / [!IMPORTANT] / [!CAUTION]
   * in blockquotes render as coloured callouts (GFM-style). */
  const baseBlockquote = renderer.blockquote.bind(renderer);
  renderer.blockquote = (body) => {
    const html = typeof body === 'string' ? body : (body && body.text) || '';
    const raw = typeof body === 'string' ? body : baseBlockquote(body);
    const match = /<p>\s*\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]\s*(<br\s*\/?>\s*)?/i
      .exec(raw || String(html));
    if (match) {
      const kind = match[1].toLowerCase();
      const inner = String(raw || html).replace(match[0], '<p>');
      return `<div class="md-alert md-alert-${kind}"><div class="md-alert-label">${kind}</div>${inner.replace(/^<blockquote>|<\/blockquote>$/g, '')}</div>`;
    }
    return baseBlockquote(body);
  };
  marked.setOptions({
    gfm: true,
    breaks: true,    /* single \n becomes <br> — matches chat expectations */
    renderer,
    headerIds: false,
    mangle: false,
  });
  CP._markedReady = true;
  return true;
};

/* Click handler for the copy button embedded in every md-code block. Uses
 * delegation from document so it survives re-renders. Called once from
 * CP.install. */
CP._bindMarkdownCopy = () => {
  if (CP._mdCopyBound) return;
  CP._mdCopyBound = true;
  document.addEventListener('click', async (ev) => {
    const btn = ev.target && ev.target.closest && ev.target.closest('.md-code-copy');
    if (!btn) return;
    const src = btn.getAttribute('data-src') || '';
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(src);
      } else {
        const ta = document.createElement('textarea');
        ta.value = src;
        ta.style.position = 'fixed'; ta.style.top = '-9999px';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta);
      }
      btn.classList.add('copied');
      const label = btn.querySelector('span');
      const orig = label ? label.textContent : '';
      if (label) label.textContent = '已复制';
      setTimeout(() => {
        btn.classList.remove('copied');
        if (label) label.textContent = orig || '复制';
      }, 1400);
    } catch (_e) { /* clipboard permission denied — ignore silently */ }
  });
};

/* Render *text* as markdown → sanitised HTML. Falls back to escaped plain
 * text (with \n → <br>) if marked isn't loaded yet. */
CP.renderMarkdown = (text) => {
  if (text == null || text === '') return '';
  const raw = String(text);
  if (typeof marked === 'undefined') {
    return CP.escapeHtml(raw).replace(/\n/g, '<br>');
  }
  CP._configureMarked();
  let html;
  try {
    html = marked.parse(raw);
  } catch (_e) {
    return CP.escapeHtml(raw).replace(/\n/g, '<br>');
  }
  if (typeof DOMPurify !== 'undefined') {
    html = DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'class'] });
  }
  return html;
};

/* Markdown-signal regex: headings, fenced code, emphasis, links, tables,
 * ordered/unordered list markers. Used to decide whether raw CLI output
 * (which would be mangled by markdown parsing of leading +/- as bullets)
 * should be force-wrapped in a fenced code block instead. */
CP._MD_SIGNAL_RE = /(^|\n)(#{1,6}\s|```|\*\*\S|__\S|^\s*\|.+\|\s*$|^\s*\d+\.\s|^\s*[-*+]\s\S)/;

/* Smart renderer for any textual output block.
 *   - If the text contains markdown signals → markdown render (AI output).
 *   - If it looks like a unified diff / has ANSI escapes → wrap in a fenced
 *     code block so markdown doesn't turn "+foo" / "- bar" into bullets,
 *     and so hljs still colours the diff or ansi_up still renders CLI
 *     colours (via CP.renderCode inside the code fence).
 *   - Otherwise plain markdown parse (mostly pass-through for prose). */
CP.renderOutput = (text) => {
  if (text == null || text === '') return '';
  const raw = String(text);
  const looksDiff = CP._DIFF_RE.test(raw);
  const hasAnsi = CP._ANSI_RE.test(raw);
  const hasMd = CP._MD_SIGNAL_RE.test(raw);
  if ((looksDiff || hasAnsi) && !hasMd) {
    const lang = looksDiff ? 'diff' : '';
    const fenced = '```' + lang + '\n' + raw.replace(/```/g, '`\u200b``') + '\n```';
    return CP.renderMarkdown(fenced);
  }
  return CP.renderMarkdown(raw);
};

CP.fmtTime = (v) => v ? String(v).replace('T', ' ').slice(0, 19) : '-';
CP.statusLabel = (s) => CP.STATUS_LABEL[s] || s || '-';
CP.phaseLabel = (p) => CP.PHASE_LABEL[p] || p;
CP.toneClass = (s) => CP.TONE_MAP[s] || 'neutral';
CP.isJobActive = (j) => j.status === 'running' || j.status === 'queued';
CP.formatLogs = (logs) => logs
  .map(i => `[${i.phase || '-'}] agent=${i.agent || '-'} exit=${i.exit_code == null ? '-' : i.exit_code}\n${i.output_excerpt || ''}`)
  .join('\n\n');

/* Network-level failure (DNS / server down / offline) surfaces as a TypeError
 * from `fetch`, with a browser-specific English message (Chrome: "Failed to
 * fetch", Firefox: "NetworkError when attempting to fetch resource."). We
 * translate those into one consistent Chinese message so the toast stack
 * doesn't leak native-locale strings. */
CP.NETWORK_ERROR = '无法连接到 CodePilot 服务，请检查后端是否仍在运行';

async function _cpFetch(url, init) {
  let r;
  try {
    r = await fetch(url, init);
  } catch (_e) {
    throw new Error(CP.NETWORK_ERROR);
  }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText || '请求失败');
  return d;
}

CP.api = {
  get(url) { return _cpFetch(url); },
  post(url, body) {
    return _cpFetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  },
  del(url) { return _cpFetch(url, { method: 'DELETE' }); },
};

/* Server-sent events client. Wraps EventSource with a small reconnect policy
 * so dashboard panels can subscribe to live progress without rolling their
 * own. Returns a handle with .close() for teardown. */
CP.sse = {
  open(url, onEvent, onError) {
    let es = null;
    let closed = false;
    let lastEventId = '';
    const connectUrl = () => {
      if (!lastEventId) return url;
      const sep = url.includes('?') ? '&' : '?';
      return `${url}${sep}last_event_id=${encodeURIComponent(lastEventId)}`;
    };
    const connect = () => {
      if (closed) return;
      try {
        es = new EventSource(connectUrl());
      } catch (err) {
        if (onError) onError(err);
        return;
      }
      es.onmessage = (ev) => {
        if (!ev || !ev.data) return;
        if (ev.lastEventId) lastEventId = String(ev.lastEventId);
        try {
          onEvent(JSON.parse(ev.data));
        } catch (err) {
          /* ignore malformed frames */
        }
      };
      es.onerror = () => {
        if (closed) return;
        try { es.close(); } catch (e) { /* ignore */ }
        /* Browser EventSource auto-reconnects on most transient failures.
         * We only re-open if the connection fully closed. */
        setTimeout(connect, 2000);
      };
    };
    connect();
    return {
      close() {
        closed = true;
        if (es) try { es.close(); } catch (e) { /* ignore */ }
      },
    };
  },
};

/* Register helper so components can use `this.$cp.fmtTime(...)` etc. */
CP.install = (app) => {
  app.config.globalProperties.$cp = {
    fmtTime: CP.fmtTime,
    statusLabel: CP.statusLabel,
    phaseLabel: CP.phaseLabel,
    toneClass: CP.toneClass,
    isJobActive: CP.isJobActive,
    formatLogs: CP.formatLogs,
  };
  /* Markdown code-block copy button needs a document-level listener —
   * install it once. No-op on subsequent calls. */
  if (CP._bindMarkdownCopy) CP._bindMarkdownCopy();
};
