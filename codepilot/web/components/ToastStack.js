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
  },
  template: `
    <div class="toast-stack">
      <div v-for="t in toasts" :key="t.id" class="toast" :class="t.type || 'info'">
        <div class="toast-body">
          {{ t.message }}
          <span v-if="t.count > 1" class="toast-count">×{{ t.count }}</span>
        </div>
        <button class="icon-btn small" @click="dismiss(t.id)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>
    </div>
  `,
});
