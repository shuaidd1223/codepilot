/* CodePilot shared constants & helpers. Exposes window.CP. */
/* global Vue */

window.CP = window.CP || { Components: {} };

CP.STATUS_LABEL = {
  backlog: '待办', in_progress: '进行中', done: '已完成', failed: '失败',
  cancelled: '已取消', archived: '已归档', queued: '排队', running: '执行中', cancelling: '停止中', succeeded: '成功',
  attention: '需关注', error: '错误', info: '信息', warning: '警告',
  question: '问题', requirement: '需求', task: '任务', command: '命令',
};

CP.PHASE_LABEL = {
  queued: '排队中', planning: '规划中', running: '执行中', cancelling: '停止中',
  done: '完成', failed: '失败', attention: '需关注',
  pending: '准备执行', preflight: '预检', dispatch: '脚本执行',
  runtime: '运行中', builder: 'Build', reviewer: 'Review',
  review: 'Review', commit: '提交', merge: '合并',
};

CP.TONE_MAP = {
  in_progress: 'info', running: 'info', planning: 'info', cancelling: 'warning', queued: 'neutral',
  backlog: 'neutral', done: 'success', succeeded: 'success',
  failed: 'danger', error: 'danger', cancelled: 'danger',
  archived: 'neutral',
  warning: 'warning', attention: 'warning',
  info: 'info', question: 'primary', requirement: 'primary', task: 'primary',
  command: 'neutral',
};

CP.TASK_PHASE_STEPS = [
  { key: 'pending', label: '准备' },
  { key: 'builder', label: 'Build' },
  { key: 'reviewer', label: 'Review' },
  { key: 'merge', label: '合并' },
  { key: 'done', label: '完成' },
];

CP._normalizeTaskPhase = (value) => String(value || '').trim().toLowerCase();
CP._taskPhaseBase = (task) => {
  const raw = CP._normalizeTaskPhase(task && task.phase);
  if (!raw) return '';
  if (raw.includes('review')) return 'reviewer';
  if (raw.includes('build')) return 'builder';
  if (raw.includes('merge')) return 'merge';
  if (raw.includes('commit')) return 'merge';
  if (raw.includes('preflight')) return 'pending';
  if (raw.includes('dispatch')) return 'builder';
  if (raw.includes('runtime') || raw.includes('running') || raw.includes('pending')) return 'pending';
  return raw.split(/\s+/)[0] || raw;
};
CP.taskPhaseProgress = (task) => {
  const t = task || {};
  const status = String(t.status || '');
  const failed = status === 'failed' || status === 'cancelled';
  const backlog = status === 'backlog' || status === 'queued';
  let key = CP._taskPhaseBase(t);
  if (status === 'done' || status === 'archived') key = 'done';
  if (!key) key = backlog ? 'pending' : (status === 'in_progress' ? 'pending' : status || 'pending');

  const stepKeys = CP.TASK_PHASE_STEPS.map(step => step.key);
  let index = stepKeys.indexOf(key);
  if (index < 0) index = 0;
  if (status === 'done' || status === 'archived') index = CP.TASK_PHASE_STEPS.length - 1;
  const maxIndex = Math.max(CP.TASK_PHASE_STEPS.length - 1, 1);
  const percent = status === 'done' || status === 'archived'
    ? 100
    : Math.max(8, Math.round((index / maxIndex) * 100));

  const steps = CP.TASK_PHASE_STEPS.map((step, idx) => {
    let state = 'pending';
    if (idx < index || status === 'done' || status === 'archived') state = 'done';
    if (idx === index && !['done', 'archived'].includes(status)) state = failed ? 'failed' : (backlog ? 'waiting' : 'current');
    return { ...step, state };
  });

  let currentLabel = CP.phaseLabel(t.phase || key);
  if (backlog) currentLabel = t.skip_reason ? '预检跳过，等待重试' : '等待执行';
  if (status === 'done') currentLabel = '任务已完成';
  if (status === 'archived') currentLabel = '任务已归档';
  if (status === 'failed') currentLabel = `失败在 ${CP.phaseLabel(t.phase || key)}`;
  if (status === 'cancelled') currentLabel = `已停止在 ${CP.phaseLabel(t.phase || key)}`;

  let nextLabel = '下一步：等待任务轮询执行';
  if (status === 'in_progress') {
    nextLabel = {
      pending: '下一步：进入 Build',
      builder: '下一步：Review 验收',
      reviewer: '下一步：合并或回到 Build 修复',
      merge: '下一步：标记完成',
    }[key] || '下一步：继续执行';
  } else if (status === 'done' || status === 'archived') {
    nextLabel = '流程已结束';
  } else if (status === 'failed' || status === 'cancelled') {
    nextLabel = '下一步：查看日志后重试或删除';
  } else if (t.skip_reason) {
    nextLabel = '下一步：修正环境后自动重试';
  }

  return {
    key,
    percent,
    steps,
    currentLabel,
    nextLabel,
    tone: failed ? 'danger' : (backlog || t.skip_reason ? 'warning' : (status === 'done' ? 'success' : 'info')),
  };
};

CP._clarifyText = (value) => String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
CP._clarifyQuestionId = (idx) => `q${idx + 1}`;
CP._clarifyOptionId = (idx) => `opt${idx + 1}`;
CP.normalizeClarifyQuestion = (question, idx = 0) => {
  if (typeof question === 'string') {
    const text = CP._clarifyText(question);
    if (!text) return null;
    return { id: CP._clarifyQuestionId(idx), type: 'text', text, options: [], allow_free_text: false };
  }
  if (!question || typeof question !== 'object') return null;
  const text = CP._clarifyText(question.text || question.question || question.label);
  if (!text) return null;
  let type = CP._clarifyText(question.type || 'text').toLowerCase();
  if (!['text', 'single', 'multi'].includes(type)) type = 'text';
  const options = Array.isArray(question.options)
    ? question.options
      .map((opt, optIdx) => {
        if (typeof opt === 'string') {
          const label = CP._clarifyText(opt);
          if (!label) return null;
          return { id: CP._clarifyOptionId(optIdx), label };
        }
        if (!opt || typeof opt !== 'object') return null;
        const label = CP._clarifyText(opt.label || opt.text || opt.value);
        if (!label) return null;
        return { id: CP._clarifyText(opt.id || opt.value || CP._clarifyOptionId(optIdx)), label };
      })
      .filter(Boolean)
    : [];
  if ((type === 'single' || type === 'multi') && !options.length) type = 'text';
  return {
    id: CP._clarifyText(question.id || question.question_id || CP._clarifyQuestionId(idx)),
    type,
    text,
    options,
    allow_free_text: type === 'text' ? false : !!(question.allow_free_text ?? true),
  };
};
CP.normalizeClarifyQuestions = (questions) =>
  (Array.isArray(questions) ? questions : []).map((q, idx) => CP.normalizeClarifyQuestion(q, idx)).filter(Boolean);
CP.createClarifyAnswerState = (questions, existing = {}) => {
  const state = {};
  for (const q of CP.normalizeClarifyQuestions(questions)) {
    const prev = existing && typeof existing === 'object' ? existing[q.id] || {} : {};
    const optionIds = new Set((q.options || []).map(opt => CP._clarifyText(opt && opt.id)).filter(Boolean));
    const selected = Array.isArray(prev.selectedOptionIds)
      ? prev.selectedOptionIds.map(x => CP._clarifyText(x)).filter(Boolean)
      : [];
    const text = CP._clarifyText(prev.text);
    const normalizedSelected = selected.filter(id => !optionIds.size || optionIds.has(id));
    state[q.id] = {
      selectedOptionIds: q.type === 'single'
        ? ((q.allow_free_text && text) ? [] : normalizedSelected.slice(0, 1))
        : normalizedSelected,
      text,
    };
  }
  return state;
};
CP.exportClarifyAnswers = (questions, answers) => {
  const out = [];
  for (const q of CP.normalizeClarifyQuestions(questions)) {
    const state = (answers && answers[q.id]) || {};
    const optionIds = new Set((q.options || []).map(opt => CP._clarifyText(opt && opt.id)).filter(Boolean));
    const selected = Array.isArray(state.selectedOptionIds)
      ? state.selectedOptionIds.map(x => CP._clarifyText(x)).filter(Boolean)
      : [];
    const text = CP._clarifyText(state.text);
    const normalizedSelected = selected.filter(id => !optionIds.size || optionIds.has(id));
    const exportedSelected = q.type === 'single' && q.allow_free_text && text
      ? []
      : (q.type === 'single' ? normalizedSelected.slice(0, 1) : normalizedSelected);
    if (!exportedSelected.length && !text) continue;
    out.push({
      question_id: q.id,
      selected_option_ids: exportedSelected,
      free_text: text,
    });
  }
  return out;
};
CP.clarifyQuestionText = (question) => {
  const q = CP.normalizeClarifyQuestion(question, 0);
  return q ? q.text : '';
};
CP.clarifyQuestionsText = (questions) => {
  const lines = [];
  for (const [idx, q] of CP.normalizeClarifyQuestions(questions).entries()) {
    lines.push(`${idx + 1}. ${q.text}`);
    if (q.type === 'single' || q.type === 'multi') {
      q.options.forEach((opt, optIdx) => lines.push(`   ${optIdx + 1}. ${opt.label}`));
      if (q.allow_free_text) lines.push('   其他：可直接手动输入文本');
    }
  }
  return lines.join('\n');
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

CP._taskTemplateContent = (item) => {
  if (!item || typeof item !== 'object') return '';
  if (item.content !== undefined && item.content !== null && item.content !== '') return String(item.content);
  if (item.body !== undefined && item.body !== null && item.body !== '') return String(item.body);
  if (item.description !== undefined && item.description !== null && item.description !== '') return String(item.description);
  return '';
};

CP._safeRegExp = (pattern, flags) => {
  try {
    return new RegExp(String(pattern || ''), String(flags || ''));
  } catch (_e) {
    return null;
  }
};

CP.validateTaskBatchImport = (raw, schema) => {
  const result = {
    valid: false,
    parseError: '',
    globalErrors: [],
    items: [],
    total: 0,
    validCount: 0,
    invalidCount: 0,
  };
  const text = String(raw == null ? '' : raw).trim();
  if (!text) return result;
  if (!schema || typeof schema !== 'object') {
    result.globalErrors.push('模板 schema 尚未就绪，暂时无法校验。');
    return result;
  }

  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    result.parseError = `JSON 解析失败：${err.message || err}`;
    return result;
  }
  if (!Array.isArray(parsed)) {
    result.globalErrors.push('顶层必须是 JSON 数组。');
    return result;
  }
  if (!parsed.length) {
    result.globalErrors.push('至少需要 1 条任务。');
    return result;
  }

  const validation = (schema && schema.validation) || {};
  const requiredHeadings = Array.isArray(validation.required_headings) ? validation.required_headings : [];
  const placeholderTokens = Array.isArray(validation.placeholder_tokens) ? validation.placeholder_tokens : [];
  const priorityValues = new Set(
    (Array.isArray(validation.priority_values) ? validation.priority_values : ['P0', 'P1', 'P2', 'P3'])
      .map(item => String(item || '').toUpperCase())
      .filter(Boolean),
  );

  result.items = parsed.map((item, idx) => {
    const entry = {
      index: idx + 1,
      raw: item,
      title: '',
      ok: false,
      errors: [],
      missingSections: [],
      leftoverPlaceholders: [],
    };
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      entry.errors.push('必须是对象。');
      return entry;
    }

    entry.title = String(item.title || item.name || '').trim();
    if (!entry.title) {
      entry.errors.push('缺少 title。');
    }

    const content = CP._taskTemplateContent(item);
    if (!content.trim()) {
      entry.errors.push('缺少 content。');
    } else {
      for (const section of requiredHeadings) {
        const rx = CP._safeRegExp(section && section.pattern, section && section.flags);
        if (!rx) continue;
        if (!rx.test(content)) {
          entry.missingSections.push(String((section && section.label) || '未知章节'));
        }
      }
      if (entry.missingSections.length) {
        entry.errors.push(`缺少章节：${entry.missingSections.join('、')}。`);
      }
      entry.leftoverPlaceholders = placeholderTokens.filter(token => token && content.includes(token));
      if (entry.leftoverPlaceholders.length) {
        entry.errors.push(`仍包含未替换占位符：${entry.leftoverPlaceholders.join('、')}。`);
      }
    }

    const priority = String(item.priority || 'P2').toUpperCase();
    if (priority && !priorityValues.has(priority)) {
      entry.errors.push(`priority 无效：${priority}。`);
    }

    entry.ok = entry.errors.length === 0;
    return entry;
  });

  result.total = result.items.length;
  result.invalidCount = result.items.filter(item => !item.ok).length;
  result.validCount = result.total - result.invalidCount;
  result.valid = !result.parseError && result.globalErrors.length === 0 && result.invalidCount === 0;
  return result;
};

CP.fmtTime = (v) => v ? String(v).replace('T', ' ').slice(0, 19) : '-';
CP.statusLabel = (s) => CP.STATUS_LABEL[s] || s || '-';
CP.phaseLabel = (p) => CP.PHASE_LABEL[p] || p;
CP.toneClass = (s) => CP.TONE_MAP[s] || 'neutral';
CP.isJobActive = (j) => ['running', 'queued', 'planning', 'cancelling'].includes(j && j.status);
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
    CP.sseLastEventIds = CP.sseLastEventIds || {};
    const streamKey = url.split('?')[0];
    let lastEventId = CP.sseLastEventIds[streamKey] || '';
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
        if (ev.lastEventId) {
          lastEventId = String(ev.lastEventId);
          CP.sseLastEventIds[streamKey] = lastEventId;
        }
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
    taskPhaseProgress: CP.taskPhaseProgress,
    toneClass: CP.toneClass,
    isJobActive: CP.isJobActive,
    formatLogs: CP.formatLogs,
  };
  /* Markdown code-block copy button needs a document-level listener —
   * install it once. No-op on subsequent calls. */
  if (CP._bindMarkdownCopy) CP._bindMarkdownCopy();
};
