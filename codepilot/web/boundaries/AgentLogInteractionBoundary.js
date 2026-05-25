/* Agent-log interaction state boundary (follow/search/scroll). */
/* global CP */

window.CP = window.CP || {};

CP.AgentLogInteractionBoundary = CP.AgentLogInteractionBoundary || (() => {
  function findSearchMatches(blocks, query) {
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

  function searchSummary(query, matches, searchPos) {
    const hasQuery = !!String(query || '').trim();
    if (!hasQuery) return '搜索';
    const total = Array.isArray(matches) ? matches.length : 0;
    if (!total) return '0/0';
    const cur = searchPos >= 0 ? searchPos + 1 : 0;
    return `${cur}/${total}`;
  }

  function linesLabel(lineCount) {
    return `${lineCount} 行`;
  }

  function jumpLabel(unreadLines) {
    return unreadLines > 0 ? `↓ 回到最新 · +${unreadLines} 行` : '↓ 回到最新';
  }

  function handleTextLengthChanged(vm) {
    if (!vm || typeof vm.$nextTick !== 'function') return;
    vm.$nextTick(() => {
      if (!vm.textLength) vm.unreadLines = 0;
      if (vm.followEnabled && vm.stickToBottom) vm._scrollToBottom();
    });
  }

  function handleLineCountChanged(vm, next, prev) {
    if (!vm) return;
    const oldVal = Number.isFinite(prev) ? prev : 0;
    const delta = Math.max(0, (Number.isFinite(next) ? next : 0) - oldVal);
    if (!delta) return;
    if (!vm.followEnabled || !vm.stickToBottom) vm.unreadLines += delta;
  }

  function handleSearchQueryChanged(vm) {
    if (!vm) return;
    vm.searchPos = -1;
  }

  function handleSearchMatchesChanged(vm, next) {
    if (!vm) return;
    const len = Array.isArray(next) ? next.length : 0;
    if (!len) {
      vm.searchPos = -1;
      return;
    }
    if (vm.searchPos >= len) vm.searchPos = 0;
  }

  function toggleFollow(vm) {
    if (!vm) return;
    vm.manualFollowPaused = !vm.manualFollowPaused;
    if (!vm.manualFollowPaused) vm.scrollToBottom();
  }

  function scrollToBlock(vm, idx) {
    if (!vm) return;
    const body = vm.$refs && vm.$refs.body;
    if (!body || !Number.isFinite(idx) || idx < 0) return;
    const node = body.querySelector(`.al-md-wrap[data-idx="${idx}"]`);
    if (!node) return;
    vm.manualFollowPaused = true;
    vm.stickToBottom = false;
    node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function nextMatch(vm) {
    if (!vm) return;
    const total = (vm.searchMatches || []).length;
    if (!total) return;
    const next = (vm.searchPos + 1 + total) % total;
    vm.searchPos = next;
    scrollToBlock(vm, vm.searchMatches[next]);
  }

  function prevMatch(vm) {
    if (!vm) return;
    const total = (vm.searchMatches || []).length;
    if (!total) return;
    const prev = (vm.searchPos - 1 + total) % total;
    vm.searchPos = prev;
    scrollToBlock(vm, vm.searchMatches[prev]);
  }

  function onSearchKeydown(vm, ev) {
    if (!vm || !ev || ev.key !== 'Enter') return;
    if (ev.shiftKey) prevMatch(vm);
    else nextMatch(vm);
  }

  function onScroll(vm) {
    if (!vm) return;
    const body = vm.$refs && vm.$refs.body;
    if (!body) return;
    const distance = body.scrollHeight - body.scrollTop - body.clientHeight;
    vm.stickToBottom = distance < 24;
    if (vm.stickToBottom) vm.unreadLines = 0;
  }

  function scrollToBottom(vm) {
    if (!vm) return;
    vm.manualFollowPaused = false;
    vm.stickToBottom = true;
    vm.unreadLines = 0;
    if (typeof vm.$nextTick === 'function') {
      vm.$nextTick(() => vm._scrollToBottom());
    }
  }

  return Object.freeze({
    findSearchMatches,
    searchSummary,
    linesLabel,
    jumpLabel,
    handleTextLengthChanged,
    handleLineCountChanged,
    handleSearchQueryChanged,
    handleSearchMatchesChanged,
    toggleFollow,
    onSearchKeydown,
    nextMatch,
    prevMatch,
    scrollToBlock,
    onScroll,
    scrollToBottom,
  });
})();
