/* Chat / session view. */
/* global Vue, CP */
CP.Components.ChatView = Vue.defineComponent({
  name: 'CpChatView',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    session() { return this.s.sessionDetail; },
    messages() { return this.s.sessionMessages; },
  },
  methods: {
    send() { this.cp.sendChat(); },
    del() { this.cp.deleteSession(); },
  },
  mounted() { this.cp.registerChatScroll(this.$refs.scroll); },
  updated() { this.cp.registerChatScroll(this.$refs.scroll); },
  beforeUnmount() { this.cp.registerChatScroll(null); },
  template: `
    <div class="view chat-view">
      <div v-if="!session" class="big-empty">会话加载中…</div>
      <template v-else>
        <div class="chat-header">
          <div class="min-w">
            <div class="chat-title">#{{ session.id }} {{ session.title }}</div>
            <div class="muted tiny">项目: {{ session.project }} · 创建 {{ $cp.fmtTime(session.created_at) }}</div>
          </div>
          <button class="btn btn-danger-outline" @click="del" :disabled="s.sending">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
            删除
          </button>
        </div>
        <div ref="scroll" class="chat-messages">
          <div v-if="!messages.length" class="chat-empty">会话刚创建，发送第一条消息开始对话</div>
          <div v-for="m in messages" :key="m.id" class="chat-row" :class="m.role">
            <div class="bubble" :class="m.role">
              <div class="bubble-body">{{ m.content }}</div>
              <div class="bubble-meta">
                <span>{{ $cp.fmtTime(m.created_at) }}</span>
                <cp-chip v-if="m.intent" tiny :tone="$cp.toneClass(m.intent)">{{ $cp.statusLabel(m.intent) }}</cp-chip>
                <span v-if="m.task_ids && m.task_ids.length">任务: <span v-for="(id, i) in m.task_ids" :key="id">#{{ id }}<span v-if="i<m.task_ids.length-1">, </span></span></span>
              </div>
            </div>
          </div>
        </div>
        <div class="chat-input-bar">
          <input v-model="s.chatText" @keydown.enter.exact.prevent="send" maxlength="4096" placeholder="在此会话中输入问题或需求…">
          <select v-model="s.chatCategory" class="w-24">
            <option value="auto">自动</option>
            <option value="question">问题</option>
            <option value="requirement">需求</option>
          </select>
          <button class="btn btn-primary" @click="send" :disabled="s.sending || !s.chatText.trim()">
            <span v-if="s.sending" class="spinner"></span>
            <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            发送
          </button>
        </div>
      </template>
    </div>
  `,
});
