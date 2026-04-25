/* Chat / session view. */
/* global Vue, CP */
CP.Components.ChatView = Vue.defineComponent({
  name: 'CpChatView',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    session() { return this.s.sessionDetail; },
    messages() { return this.s.sessionMessages; },
    chatPending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.SESSION_SEND); },
    clarifyPending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.SESSION_CLARIFY_REPLY); },
    clarifyCancelPending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.SESSION_CLARIFY_CANCEL); },
    deletePending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.SESSION_DELETE); },
    pendingClarify() {
      if (!this.session) return null;
      return this.s.clarifyDrafts[this.session.id] || null;
    },
  },
  methods: {
    send() { this.cp.sendChat(); },
    del() { this.cp.deleteSession(); },
    async sendClarify() {
      if (!this.session) return;
      await this.cp.submitClarifyAnswer(this.session.id);
    },
    updateClarifyAnswers(nextAnswers) {
      if (this.pendingClarify) this.pendingClarify.answers = nextAnswers;
    },
    cancelClarify() {
      if (!this.session) return;
      this.cp.cancelSessionClarify(this.session.id);
    },
  },
  mounted() { this.cp.registerChatScroll(this.$refs.scroll); },
  updated() { this.cp.registerChatScroll(this.$refs.scroll); },
  beforeUnmount() { this.cp.registerChatScroll(null); },
  template: `
    <div class="view chat-view">
      <div v-if="!session" class="big-empty">会话加载中…</div>
      <template v-else>
        <div class="chat-header chat-header-compact">
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
        <div ref="scroll" class="chat-messages">
          <div v-if="!messages.length" class="chat-empty">会话刚创建，发送第一条消息开始对话</div>
          <div v-for="m in messages" :key="m.id" class="chat-row" :class="m.role">
            <div class="bubble" :class="[m.role, m.intent === 'clarify' ? 'clarify' : '']">
              <div v-if="m.intent === 'clarify'" class="clarify-head">🤔 需要澄清几个点</div>
              <cp-markdown class="bubble-body" :text="m.content"></cp-markdown>
              <div class="bubble-meta">
                <span>{{ $cp.fmtTime(m.created_at) }}</span>
                <cp-chip v-if="m.intent" tiny :tone="$cp.toneClass(m.intent)">{{ $cp.statusLabel(m.intent) }}</cp-chip>
                <span v-if="m.task_ids && m.task_ids.length">任务: <span v-for="(id, i) in m.task_ids" :key="id">#{{ id }}<span v-if="i<m.task_ids.length-1">, </span></span></span>
              </div>
            </div>
          </div>
        </div>
        <!-- Quick-reply for outstanding clarification -->
        <div v-if="pendingClarify" class="clarify-panel" style="border-top:1px solid var(--border);padding:12px;background:var(--bg-subtle, #fafafa)">
          <div class="muted tiny" style="margin-bottom:8px">
            当前会话正在等待你补充澄清信息。单选/多选题可直接选，也可以手动输入文本。
          </div>
          <cp-clarify-fields
            :questions="pendingClarify.questions"
            :answers="pendingClarify.answers"
            @update:answers="updateClarifyAnswers"
          ></cp-clarify-fields>
          <div class="row gap-sm end" style="margin-top:12px">
            <button class="btn btn-outline" @click="cancelClarify" :disabled="clarifyCancelPending">
              取消本次规划
            </button>
            <button class="btn btn-primary" @click="sendClarify" :disabled="clarifyPending">
              <span v-if="clarifyPending" class="spinner"></span>
              回答
            </button>
          </div>
        </div>
        <div class="chat-input-bar">
          <input
            v-model="s.chatText"
            @keydown.enter.exact.prevent="send"
            maxlength="4096"
            :disabled="!!pendingClarify"
            :placeholder="pendingClarify ? '当前会话正在等待澄清回答，可在上方填写或取消' : '在此会话中输入问题或需求…'"
          >
          <select v-model="s.chatCategory" class="w-24">
            <option value="auto">自动</option>
            <option value="question">问题</option>
            <option value="requirement">需求</option>
          </select>
          <button class="btn btn-primary" @click="send" :disabled="chatPending || !!pendingClarify || !s.chatText.trim()">
            <span v-if="chatPending" class="spinner"></span>
            <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            发送
          </button>
        </div>
      </template>
    </div>
  `,
});
