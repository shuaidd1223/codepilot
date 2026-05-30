/* Shared session chat panel + routed advanced session view.
 * Multi-line input, @file references, file upload/drag-drop/paste. */
/* global Vue, CP */
CP.Components.SessionChatPanel = Vue.defineComponent({
  name: 'CpSessionChatPanel',
  inject: ['cp'],
  props: {
    embedded: { type: Boolean, default: false },
  },
  data() {
    return {
      attachedFiles: [],       // {name, data(base64), size, project}
      mentionQuery: '',        // 当前 @ 后的搜索词
      mentionResults: [],      // 搜索结果 [{path, name}]
      mentionActive: false,    // 是否显示 @ 下拉
      mentionIdx: -1,          // 下拉选中索引
      dragOver: false,         // 拖拽悬停状态
      fileInputKey: 0,         // 用于重置 file input
      _mentionTimer: null,     // 搜索防抖定时器
      _lastMentionPos: -1,     // @ 在文本中的位置
    };
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
    agentModeLabel() {
      return CP.AGENT_MODE_LABELS[this.runtime.agentMode || 'codepilot'] || 'CodePilot';
    },
    agentModeOptions() { return CP.AGENT_MODE_OPTIONS; },
    permissionModeLabel() {
      return CP.PERMISSION_MODE_LABELS[this.runtime.permissionMode || 'ask'] || 'Ask';
    },
    permissionModeOptions() { return CP.PERMISSION_MODE_OPTIONS; },
    permissionModeHints() {
      return CP.PERMISSION_MODE_OPTIONS.reduce((acc, opt) => { acc[opt.key] = opt.hint; return acc; }, {});
    },
    hasFiles() { return this.attachedFiles && this.attachedFiles.length > 0; },
    canSend() { return this.chatPending || (!this.s.chatText.trim() && !this.hasFiles); },
    activeProject() { return this.s.nav && this.s.nav.project; },
  },
  methods: {
    // ── 发送消息 ──
    send() {
      if (this.activeRun) return;
      if (!this.s.chatText.trim() && !this.hasFiles) return;
      // 关闭 @ 下拉
      this.mentionActive = false;
      // 构造消息体
      const msg = { text: this.s.chatText, files: this.attachedFiles.map(f => ({
        ...f,
        project: this.activeProject,
      })) };
      // 存回 state 供 sendChat/sendEmbeddedChat 使用
      this.s._pendingFiles = msg.files;
      if (this.embedded) this.cp.sendEmbeddedChat();
      else this.cp.sendChat();
      this.attachedFiles = [];
      this.s.chatText = '';
    },
    onKeydown(e) {
      // 合并 Enter/mention 快捷键处理，避免重复 @keydown 属性
      this.onMentionKeydown(e);
      if (!e.defaultPrevented) this.sendOnEnter(e);
    },
    sendOnEnter(e) {
      // Enter 发送，Shift+Enter 换行
      if (e.key !== 'Enter') return; // 非 Enter 键不拦截，让正常输入/删除通过
      if (e.shiftKey) return; // 让 textarea 自然换行
      e.preventDefault();
      this.send();
    },

    // ── 停止生成 ──
    stopSessionRun() { this.cp.stopSessionRun(); },

    // ── 会话管理 ──
    newEmbeddedSession() { this.cp.newSession(this.s.nav.project); },
    openAdvanced() {
      if (!this.session) return;
      this.cp.openSessionPage(this.session.project || this.s.nav.project, this.session.id);
    },
    selectEmbeddedSession(event) {
      const id = Number(event && event.target && event.target.value);
      if (id) this.cp.selectEmbeddedSession(this.s.nav.project, id);
    },
    setAgentMode(mode) { this.s.opencodeRuntime.agentMode = mode; },
    setPermissionMode(mode) {
      const project = this.s.nav.project;
      if (!project) return;
      this.s.opencodeRuntime.permissionMode = mode;
      CP.api.post(`/api/projects/${encodeURIComponent(project)}/permission`, { mode }).then((res) => {
        if (res && res.mode) this.s.opencodeRuntime.permissionMode = res.mode;
      }).catch((err) => {
        console.error('更新权限模式失败:', err);
      });
    },
    del() { this.cp.deleteSession(); },

    // ── 消息渲染 ──
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
        const body = type === 'error' ? (String(extra.error || '') || extra.content_delta || item.message) : (extra.content_delta || item.message || extra.error || '');
        return `[${ts}] ${type}${body ? ' ' + body : ''}`;
      }).join('\n');
    },
    toolNames(run) {
      const tools = (run && Array.isArray(run.tool_calls)) ? run.tool_calls : [];
      return tools.map((tool) => tool.name || tool.tool || '-').filter(Boolean);
    },
    fileSizeLabel(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / 1048576).toFixed(1) + ' MB';
    },

    // ── @ 文件引用 ──
    onInput(e) {
      const text = e.target.value;
      const pos = e.target.selectionStart;
      // 查找光标前的最后一个 @
      const before = text.slice(0, pos);
      const atIdx = before.lastIndexOf('@');
      if (atIdx >= 0) {
        const afterAt = before.slice(atIdx + 1);
        // 如果 @ 后面没有空格/换行，且是最近输入的，触发搜索
        if (!/[\s\n]/.test(afterAt) && (pos - atIdx) <= 50) {
          this._lastMentionPos = atIdx;
          this.mentionQuery = afterAt;
          this._searchMentionDebounced();
          return;
        }
      }
      this.mentionActive = false;
    },
    _searchMentionDebounced() {
      if (this._mentionTimer) clearTimeout(this._mentionTimer);
      this._mentionTimer = setTimeout(() => this._searchMention(), 200);
    },
    _searchMention() {
      const project = this.activeProject;
      if (!project) { this.mentionActive = false; return; }
      const q = this.mentionQuery;
      CP.api.get(`/api/projects/${encodeURIComponent(project)}/files/search?q=${encodeURIComponent(q)}&_t=${Date.now()}`)
        .then((res) => {
          if (res && res.files) {
            this.mentionResults = res.files;
            this.mentionActive = res.files.length > 0;
            this.mentionIdx = -1;
          } else {
            this.mentionActive = false;
          }
        }).catch(() => { this.mentionActive = false; });
    },
    selectMention(file) {
      const ta = this.$refs.chatTextarea;
      if (!ta) return;
      const text = this.s.chatText;
      const before = text.slice(0, this._lastMentionPos);
      const after = text.slice(ta.selectionEnd || this._lastMentionPos + this.mentionQuery.length + 1);
      // 插入 @path/to/file 并加空格
      this.s.chatText = before + '@' + file.path + ' ' + after;
      this.mentionActive = false;
      this.$nextTick(() => {
        ta.focus();
        const newPos = before.length + file.path.length + 2;
        ta.setSelectionRange(newPos, newPos);
      });
    },
    onMentionKeydown(e) {
      if (!this.mentionActive) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        this.mentionIdx = Math.min(this.mentionIdx + 1, this.mentionResults.length - 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        this.mentionIdx = Math.max(this.mentionIdx - 1, 0);
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        if (this.mentionIdx >= 0 && this.mentionResults[this.mentionIdx]) {
          e.preventDefault();
          this.selectMention(this.mentionResults[this.mentionIdx]);
        }
      } else if (e.key === 'Escape') {
        this.mentionActive = false;
      }
    },

    // ── 文件上传 ──
    uploadFiles(fileList) {
      if (!fileList || !fileList.length) return;
      const readerNext = (i) => {
        if (i >= fileList.length) return;
        const file = fileList[i];
        if (file.size > 10 * 1024 * 1024) { // 10MB 限制
          this.cp.showToast(`文件 ${file.name} 超过 10MB 限制`, 'error');
          readerNext(i + 1);
          return;
        }
        const reader = new FileReader();
        reader.onload = (e) => {
          const base64 = e.target.result.split(',')[1]; // 去掉 data:...;base64, 前缀
          this.attachedFiles.push({
            name: file.name,
            data: base64,
            size: file.size,
            project: this.activeProject,
          });
          readerNext(i + 1);
        };
        reader.onerror = () => { readerNext(i + 1); };
        reader.readAsDataURL(file);
      };
      readerNext(0);
    },
    onFilePaste(e) {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      const files = [];
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file') {
          const file = item.getAsFile();
          if (file) files.push(file);
        }
      }
      if (files.length) {
        e.preventDefault();
        this.uploadFiles(files);
      }
      // 文本粘贴由 textarea 原生处理
    },
    onFileInputChange(e) {
      if (e.target && e.target.files) {
        this.uploadFiles(e.target.files);
      }
      this.fileInputKey++;
    },
    removeFile(idx) {
      this.attachedFiles.splice(idx, 1);
    },
    onDragOver(e) {
      e.preventDefault();
      this.dragOver = true;
    },
    onDragLeave(e) {
      e.preventDefault();
      this.dragOver = false;
    },
    onDrop(e) {
      e.preventDefault();
      this.dragOver = false;
      if (e.dataTransfer && e.dataTransfer.files) {
        this.uploadFiles(e.dataTransfer.files);
      }
    },
    triggerFilePicker() {
      this.$refs.fileInput && this.$refs.fileInput.click();
    },
  },
  mounted() {
    this.cp.registerChatScroll(this.$refs.scroll);
    // 确保 state 上有 chatText
    if (this.s.chatText === undefined) this.s.chatText = '';
  },
  updated() { this.cp.registerChatScroll(this.$refs.scroll); },
  beforeUnmount() {
    this.cp.registerChatScroll(null);
    if (this._mentionTimer) clearTimeout(this._mentionTimer);
  },
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
        <span class="runtime-label">Agent</span>
        <div class="runtime-mode-tabs">
          <button
            v-for="opt in agentModeOptions"
            :key="opt.key"
            type="button"
            :class="{active: (runtime.agentMode || 'codepilot') === opt.key}"
            :title="opt.hint"
            @click="setAgentMode(opt.key)"
          >{{ opt.label }}</button>
        </div>
      </div>
      <div v-if="embedded" class="runtime-control-bar">
        <span class="runtime-label">权限</span>
        <select
          class="permission-select"
          :value="runtime.permissionMode || 'ask'"
          :title="(permissionModeHints[runtime.permissionMode || 'ask'] || 'OpenCode 权限策略') + '（即时生效，下条消息起效）'"
          @change="setPermissionMode($event.target.value)"
        >
          <option
            v-for="opt in permissionModeOptions"
            :key="opt.key"
            :value="opt.key"
          >{{ opt.label }}</option>
        </select>
      </div>
      <div v-if="embedded" class="session-live-status" :class="{running: !!activeRun}">
        <span class="dot" :class="{on: !!activeRun}"></span>
        <span>{{ liveStatusLabel }}</span>
        <span class="muted tiny">Agent：{{ agentModeLabel }} · 权限：{{ permissionModeLabel }}</span>
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
            <div class="bubble" :class="[m.role, m.intent === 'streaming' ? 'streaming' : '']">
              <div v-if="m.intent === 'opencode' || m.intent === 'streaming'" class="agent-head">
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

        <!-- 拖拽上传遮罩 -->
        <div v-if="dragOver" class="chat-drag-overlay" @dragover.prevent @dragleave.prevent="onDragLeave" @drop.prevent="onDrop">
          <div class="chat-drag-hint">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
            <br>释放以上传文件
          </div>
        </div>

        <!-- 输入区域 -->
        <div class="chat-input-area" :class="{ 'is-streaming': !!activeRun }"
             @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop">
          <!-- 附件文件列表 -->
          <div v-if="hasFiles" class="chat-attachments">
            <div v-for="(f, i) in attachedFiles" :key="i" class="chat-attach-chip" :title="f.name + ' (' + fileSizeLabel(f.size) + ')'">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span class="attach-name">{{ f.name }}</span>
              <span class="attach-size">{{ fileSizeLabel(f.size) }}</span>
              <button class="attach-remove" @click="removeFile(i)" title="移除">&times;</button>
            </div>
          </div>
          <!-- @ 文件引用下拉 -->
          <div v-if="mentionActive && mentionResults.length" class="chat-mention-dropdown">
            <div
              v-for="(file, i) in mentionResults"
              :key="file.path"
              class="chat-mention-item"
              :class="{active: mentionIdx === i}"
              @click="selectMention(file)"
              @mouseenter="mentionIdx = i"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
              <span class="mention-path">{{ file.path }}</span>
            </div>
          </div>
          <!-- textarea + 工具栏 -->
          <div class="chat-input-flex">
            <textarea
              ref="chatTextarea"
              class="chat-textarea"
              v-model="s.chatText"
              @keydown="onKeydown"
              @input="onInput"
              @paste="onFilePaste"
              maxlength="4096"
              rows="1"
              placeholder="输入问题、需求或操作指令… @ 引用文件，Enter 发送，Shift+Enter 换行"
            ></textarea>
            <div class="chat-input-actions">
              <button class="btn-icon" @click="triggerFilePicker" title="上传文件（支持拖拽和粘贴图片）">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
              </button>
              <input type="file" ref="fileInput" class="hidden-file-input" :key="fileInputKey" multiple @change="onFileInputChange" />
              <button v-if="activeRun" class="btn btn-warning btn-sm" @click="stopSessionRun">停止</button>
              <button class="btn btn-primary btn-sm" @click="send" :disabled="canSend">
                <span v-if="chatPending" class="spinner"></span>
                <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
                发送
              </button>
            </div>
          </div>
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
