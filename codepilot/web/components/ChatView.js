/* Shared session chat panel + routed advanced session view. */
/* global Vue, CP */
CP.Components.SessionChatPanel = Vue.defineComponent({
  name: 'CpSessionChatPanel',
  inject: ['cp'],
  props: {
    embedded: { type: Boolean, default: false },
  },
  computed: {
    s() { return this.cp.state; },
    session() { return this.s.sessionDetail; },
    messages() { return this.s.sessionMessages; },
    projectSessions() { return this.cp.projectSessions || []; },
    chatPending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.SESSION_SEND); },
    deletePending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.SESSION_DELETE); },
    activeRun() { return this.cp.activeSessionRun; },
    runtime() { return this.s.opencodeRuntime || {}; },
    liveStatusLabel() {
      if (this.activeRun) return 'OpenCode 正在执行';
      if (!this.session) return '等待创建会话';
      return 'OpenCode 就绪';
    },
    taskModeLabel() {
      const labels = { chat: '问答', requirement: '需求规划', task: '完整任务', task_ai: 'AI 补全', batch: '批量导入' };
      return labels[this.runtime.taskMode || 'chat'] || '问答';
    },
  },
  methods: {
    send() {
      if (this.embedded) this.cp.sendEmbeddedChat();
      else this.cp.sendChat();
    },
    stopSessionRun() { this.cp.stopSessionRun(); },
    newEmbeddedSession() { this.cp.newSession(this.s.nav.project); },
    openAdvanced() {
      if (!this.session) return;
      this.cp.openSessionPage(this.session.project || this.s.nav.project, this.session.id);
    },
    selectEmbeddedSession(event) {
      const id = Number(event && event.target && event.target.value);
      if (id) this.cp.selectEmbeddedSession(this.s.nav.project, id);
    },
    setTaskMode(mode) {
      this.s.opencodeRuntime.taskMode = mode;
    },
    del() { this.cp.deleteSession(); },
    sessionRunForMessage(m) { return this.cp.sessionRunForMessage(m); },
    runStatusLabel(run) {
      const status = run && run.status;
      return { running: '生成中', done: '已完成', error: '失败', cancelled: '已停止' }[status] || '处理中';
    },
    runLogText(run) {
      if (!run) return '';
      if (Array.isArray(run.raw_log) && run.raw_log.length) return run.raw_log.join('\n');
      return (run.events || []).map((item) => {
        const ts = (item.timestamp || '').slice(11, 19) || '--:--:--';
        const type = item.type || 'summary';
        const extra = item.extra || {};
        const body = extra.content_delta || item.message || extra.error || '';
        return `[${ts}] ${type}${body ? ' ' + body : ''}`;
      }).join('\n');
    },
    toolNames(run) {
      const tools = (run && Array.isArray(run.tool_calls)) ? run.tool_calls : [];
      return tools.map((tool) => tool.name || tool.tool || '-').filter(Boolean);
    },
  },
  mounted() { this.cp.registerChatScroll(this.$refs.scroll); },
  updated() { this.cp.registerChatScroll(this.$refs.scroll); },
  beforeUnmount() { this.cp.registerChatScroll(null); },
  template: `
    <div class="session-chat-panel" :class="{embedded: embedded, page: !embedded}">
      <div v-if="embedded" class="project-session-toolbar">
        <div class="min-w grow">
          <h3>OpenCode</h3>
          <p class="muted">{{ liveStatusLabel }}</p>
        </div>
        <div class="toolbar-actions">
          <button class="btn btn-outline btn-sm" @click="newEmbeddedSession" :disabled="s.newSessionLoading">
            <span v-if="s.newSessionLoading" class="spinner"></span>
            新建会话
          </button>
          <button class="btn btn-danger-outline btn-sm" @click="stopSessionRun" :disabled="!activeRun">
            终止会话
          </button>
          <button class="btn btn-outline btn-sm" @click="openAdvanced" :disabled="!session">
            会话详情
          </button>
        </div>
      </div>
      <div v-if="embedded" class="runtime-control-bar">
        <span class="runtime-label">工作类型</span>
        <div class="runtime-mode-tabs">
          <button type="button" :class="{active: runtime.taskMode === 'chat' || !runtime.taskMode}" @click="setTaskMode('chat')">问答</button>
          <button type="button" :class="{active: runtime.taskMode === 'requirement'}" @click="setTaskMode('requirement')">需求规划</button>
          <button type="button" :class="{active: runtime.taskMode === 'task'}" @click="setTaskMode('task')">完整任务</button>
          <button type="button" :class="{active: runtime.taskMode === 'task_ai'}" @click="setTaskMode('task_ai')">AI 补全</button>
          <button type="button" :class="{active: runtime.taskMode === 'batch'}" @click="setTaskMode('batch')">批量导入</button>
        </div>
      </div>
      <div v-if="embedded" class="session-live-status" :class="{running: !!activeRun}">
        <span class="dot" :class="{on: !!activeRun}"></span>
        <span>{{ liveStatusLabel }}</span>
        <span class="muted tiny">工作类型：{{ taskModeLabel }}</span>
      </div>

      <div v-if="!embedded && !session" class="big-empty">会话加载中…</div>
      <template v-else>
        <div v-if="!embedded" class="chat-header chat-header-compact">
          <div class="min-w grow">
            <div class="chat-title">#{{ session.id }} {{ session.title }}</div>
            <div class="chip-row mt-xs">
              <cp-chip :tone="session.status === 'active' ? 'success' : $cp.toneClass(session.status)">
                {{ session.status === 'active' ? '会话中' : ($cp.statusLabel(session.status) || session.status || '-') }}
              </cp-chip>
              <cp-chip>消息 {{ messages.length }}</cp-chip>
            </div>
            <div class="task-kv-grid chat-kv-grid">
              <div class="task-kv-item">
                <span class="task-kv-key">项目</span>
                <span class="task-kv-value">{{ session.project || '-' }}</span>
              </div>
              <div class="task-kv-item">
                <span class="task-kv-key">创建时间</span>
                <span class="task-kv-value">{{ $cp.fmtTime(session.created_at) }}</span>
              </div>
              <div class="task-kv-item">
                <span class="task-kv-key">更新时间</span>
                <span class="task-kv-value">{{ $cp.fmtTime(session.updated_at) }}</span>
              </div>
            </div>
          </div>
          <button class="btn btn-danger-outline btn-sm" @click="del" :disabled="deletePending">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
            删除
          </button>
        </div>
        <div ref="scroll" class="chat-messages" :class="{'embedded-messages': embedded}">
          <div v-if="!messages.length" class="chat-empty">
            {{ session ? '会话刚创建，发送第一条消息开始对话' : '还没有会话，直接输入即可创建并开始流式回复' }}
          </div>
          <div v-for="m in messages" :key="m.id" class="chat-row" :class="m.role">
            <div class="bubble" :class="[m.role, m.intent === 'clarify' ? 'clarify' : '', m.intent === 'streaming' ? 'streaming' : '']">
              <div v-if="m.intent === 'opencode' || m.intent === 'streaming'" class="clarify-head">
                OpenCode
                <span v-if="sessionRunForMessage(m)" class="session-run-state">{{ runStatusLabel(sessionRunForMessage(m)) }}</span>
              </div>
              <cp-markdown class="bubble-body" :text="m.content || (m.intent === 'streaming' ? '正在生成回复…' : '')"></cp-markdown>
              <div v-if="m.role === 'assistant' && sessionRunForMessage(m)" class="session-process-panel">
                <div class="session-process-summary">
                  <span class="dot" :class="{on: sessionRunForMessage(m).status === 'running'}"></span>
                  <span>{{ runStatusLabel(sessionRunForMessage(m)) }}</span>
                  <span v-if="toolNames(sessionRunForMessage(m)).length" class="muted tiny">
                    工具 {{ toolNames(sessionRunForMessage(m)).join(' / ') }}
                  </span>
                </div>
                <details class="session-process-raw">
                  <summary>过程日志</summary>
                  <cp-agent-log
                    :text="runLogText(sessionRunForMessage(m))"
                    :done="sessionRunForMessage(m).status !== 'running'"
                    title="OpenCode · session stream"
                    follow></cp-agent-log>
                </details>
              </div>
              <div class="bubble-meta">
                <span>{{ $cp.fmtTime(m.created_at) }}</span>
                <cp-chip v-if="m.intent" tiny :tone="$cp.toneClass(m.intent)">{{ $cp.statusLabel(m.intent) }}</cp-chip>
                <span v-if="m.task_ids && m.task_ids.length">任务: <span v-for="(id, i) in m.task_ids" :key="id">#{{ id }}<span v-if="i<m.task_ids.length-1">, </span></span></span>
              </div>
            </div>
          </div>
        </div>
        <div class="chat-input-bar" :class="{ 'is-streaming': !!activeRun }">
          <input
            v-model="s.chatText"
            @keydown.enter.exact.prevent="send"
            maxlength="4096"
            placeholder="像使用 OpenCode 一样输入问题、需求或操作指令…"
          >
          <button v-if="activeRun" class="btn btn-warning" @click="stopSessionRun">
            停止生成
          </button>
          <button class="btn btn-primary" @click="send" :disabled="chatPending || !s.chatText.trim()">
            <span v-if="chatPending" class="spinner"></span>
            <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            发送
          </button>
        </div>
      </template>
    </div>
  `,
});

CP.Components.ChatView = Vue.defineComponent({
  name: 'CpChatView',
  template: `
    <div class="view chat-view">
      <cp-session-chat-panel></cp-session-chat-panel>
    </div>
  `,
});
