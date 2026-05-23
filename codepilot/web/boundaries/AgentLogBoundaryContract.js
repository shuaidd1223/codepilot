/* Agent-log adapter contract between the component and boundary modules. */
/* global CP */

window.CP = window.CP || {};

CP.AgentLogBoundaryContract = CP.AgentLogBoundaryContract || (() => {
  function _escapeHtml(text) {
    const esc = CP.escapeHtml || ((t) => String(t)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;'));
    return esc(text);
  }

  function _defaultCreateMarkdownCache() {
    return new Map();
  }

  function _defaultRenderMarkdown(_cache, raw) {
    const text = String(raw || '');
    return (CP.renderOutput
      ? CP.renderOutput(text)
      : _escapeHtml(text).replace(/\n/g, '<br>'));
  }

  function _defaultParseMarkdownBlocks(raw, renderBlockMarkdown) {
    const text = String(raw || '');
    if (!text) return [];
    const render = typeof renderBlockMarkdown === 'function'
      ? renderBlockMarkdown
      : (chunk) => _defaultRenderMarkdown(null, chunk);
    const lines = text.split(/\r?\n/).length;
    return [{
      type: 'markdown',
      key: `md:0:0:${text.length}`,
      raw: text,
      lineCount: lines,
      html: render(text),
    }];
  }

  function _defaultFindSearchMatches(blocks, query) {
    const q = String(query || '').trim().toLowerCase();
    if (!q) return [];
    const out = [];
    const list = Array.isArray(blocks) ? blocks : [];
    for (let i = 0; i < list.length; i++) {
      const raw = String((list[i] && list[i].raw) || '').toLowerCase();
      if (raw.includes(q)) out.push(i);
    }
    return out;
  }

  function _defaultSearchSummary(query, matches, searchPos) {
    const hasQuery = !!String(query || '').trim();
    if (!hasQuery) return '搜索';
    const total = Array.isArray(matches) ? matches.length : 0;
    if (!total) return '0/0';
    const cur = searchPos >= 0 ? searchPos + 1 : 0;
    return `${cur}/${total}`;
  }

  function _defaultLinesLabel(lineCount) {
    return `${lineCount} 行`;
  }

  function _defaultJumpLabel(unreadLines) {
    return unreadLines > 0 ? `↓ 回到最新 · +${unreadLines} 行` : '↓ 回到最新';
  }

  function _defaultHandleTextLengthChanged(vm) {
    if (!vm || typeof vm.$nextTick !== 'function') return;
    vm.$nextTick(() => {
      if (!vm.textLength) vm.unreadLines = 0;
      if (vm.followEnabled && vm.stickToBottom) vm._scrollToBottom();
    });
  }

  function _defaultHandleLineCountChanged(vm, next, prev) {
    if (!vm) return;
    const oldVal = Number.isFinite(prev) ? prev : 0;
    const delta = Math.max(0, (Number.isFinite(next) ? next : 0) - oldVal);
    if (!delta) return;
    if (!vm.followEnabled || !vm.stickToBottom) vm.unreadLines += delta;
  }

  function _defaultHandleSearchQueryChanged(vm) {
    if (!vm) return;
    vm.searchPos = -1;
  }

  function _defaultHandleSearchMatchesChanged(vm, next) {
    if (!vm) return;
    const len = Array.isArray(next) ? next.length : 0;
    if (!len) {
      vm.searchPos = -1;
      return;
    }
    if (vm.searchPos >= len) vm.searchPos = 0;
  }

  function _defaultToggleFollow(vm) {
    if (!vm) return;
    vm.manualFollowPaused = !vm.manualFollowPaused;
    if (!vm.manualFollowPaused) vm.scrollToBottom();
  }

  function _defaultScrollToBlock(vm, idx) {
    if (!vm) return;
    const body = vm.$refs && vm.$refs.body;
    if (!body || !Number.isFinite(idx) || idx < 0) return;
    const node = body.querySelector(`.al-md-wrap[data-idx="${idx}"]`);
    if (!node) return;
    vm.manualFollowPaused = true;
    vm.stickToBottom = false;
    node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function _defaultNextMatch(vm) {
    if (!vm) return;
    const total = (vm.searchMatches || []).length;
    if (!total) return;
    const next = (vm.searchPos + 1 + total) % total;
    vm.searchPos = next;
    _defaultScrollToBlock(vm, vm.searchMatches[next]);
  }

  function _defaultPrevMatch(vm) {
    if (!vm) return;
    const total = (vm.searchMatches || []).length;
    if (!total) return;
    const prev = (vm.searchPos - 1 + total) % total;
    vm.searchPos = prev;
    _defaultScrollToBlock(vm, vm.searchMatches[prev]);
  }

  function _defaultOnSearchKeydown(vm, ev) {
    if (!vm || !ev || ev.key !== 'Enter') return;
    if (ev.shiftKey) _defaultPrevMatch(vm);
    else _defaultNextMatch(vm);
  }

  function _defaultOnScroll(vm) {
    if (!vm) return;
    const body = vm.$refs && vm.$refs.body;
    if (!body) return;
    const distance = body.scrollHeight - body.scrollTop - body.clientHeight;
    vm.stickToBottom = distance < 24;
    if (vm.stickToBottom) vm.unreadLines = 0;
  }

  function _defaultScrollToBottom(vm) {
    if (!vm) return;
    vm.manualFollowPaused = false;
    vm.stickToBottom = true;
    vm.unreadLines = 0;
    if (typeof vm.$nextTick === 'function') {
      vm.$nextTick(() => vm._scrollToBottom());
    }
  }

  function _pick(candidate, fallback) {
    return typeof candidate === 'function' ? candidate : fallback;
  }

  function createAdapter(options = {}) {
    const renderBoundary = (options && options.renderBoundary) || {};
    const interactionBoundary = (options && options.interactionBoundary) || {};

    const render = Object.freeze({
      createMarkdownCache: _pick(renderBoundary.createMarkdownCache, _defaultCreateMarkdownCache),
      renderMarkdown: _pick(renderBoundary.renderMarkdown, _defaultRenderMarkdown),
      parseMarkdownBlocks: _pick(renderBoundary.parseMarkdownBlocks, _defaultParseMarkdownBlocks),
    });

    const interaction = Object.freeze({
      findSearchMatches: _pick(interactionBoundary.findSearchMatches, _defaultFindSearchMatches),
      searchSummary: _pick(interactionBoundary.searchSummary, _defaultSearchSummary),
      linesLabel: _pick(interactionBoundary.linesLabel, _defaultLinesLabel),
      jumpLabel: _pick(interactionBoundary.jumpLabel, _defaultJumpLabel),
      handleTextLengthChanged: _pick(interactionBoundary.handleTextLengthChanged, _defaultHandleTextLengthChanged),
      handleLineCountChanged: _pick(interactionBoundary.handleLineCountChanged, _defaultHandleLineCountChanged),
      handleSearchQueryChanged: _pick(interactionBoundary.handleSearchQueryChanged, _defaultHandleSearchQueryChanged),
      handleSearchMatchesChanged: _pick(interactionBoundary.handleSearchMatchesChanged, _defaultHandleSearchMatchesChanged),
      toggleFollow: _pick(interactionBoundary.toggleFollow, _defaultToggleFollow),
      onSearchKeydown: _pick(interactionBoundary.onSearchKeydown, _defaultOnSearchKeydown),
      nextMatch: _pick(interactionBoundary.nextMatch, _defaultNextMatch),
      prevMatch: _pick(interactionBoundary.prevMatch, _defaultPrevMatch),
      scrollToBlock: _pick(interactionBoundary.scrollToBlock, _defaultScrollToBlock),
      onScroll: _pick(interactionBoundary.onScroll, _defaultOnScroll),
      scrollToBottom: _pick(interactionBoundary.scrollToBottom, _defaultScrollToBottom),
    });

    return Object.freeze({ render, interaction });
  }

  return Object.freeze({
    createAdapter,
  });
})();
