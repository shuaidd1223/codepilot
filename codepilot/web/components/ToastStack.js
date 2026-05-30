/* Floating toast stack. */
/* global Vue, CP */
CP.Components.ToastStack = Vue.defineComponent({
  name: 'CpToastStack',
  inject: ['cp'],
  computed: {
    toasts() { return this.cp.state.toasts; },
  },
  methods: {
    dismiss(id) { this.cp.dismissToast(id); },
    tone(t) { return t.type || 'info'; },
    title(t) {
      return {
        success: '操作成功',
        error: '操作失败',
        warning: '请注意',
        info: '通知',
      }[this.tone(t)] || '通知';
    },
  },
  template: `
    <div class="toast-stack" aria-live="polite" aria-atomic="false">
      <article v-for="t in toasts" :key="t.id" class="toast" :class="tone(t)" :role="tone(t) === 'error' ? 'alert' : 'status'">
        <div class="toast-icon" aria-hidden="true">
          <svg v-if="tone(t) === 'success'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
          <svg v-else-if="tone(t) === 'error'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          <svg v-else-if="tone(t) === 'warning'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
          <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16v-4M12 8h.01"/><circle cx="12" cy="12" r="10"/></svg>
        </div>
        <div class="toast-content">
          <div class="toast-title-row">
            <div class="toast-title">{{ title(t) }}</div>
            <span v-if="t.count > 1" class="toast-count">×{{ t.count }}</span>
          </div>
          <div class="toast-body">{{ t.message }}</div>
        </div>
        <button class="icon-btn small toast-close" @click="dismiss(t.id)" :aria-label="'关闭' + title(t)" title="关闭通知">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </article>
    </div>
  `,
});
