/* Generic in-app confirm dialog; replaces browser native confirm(). */
/* global Vue, CP */
CP.Components.ConfirmDialog = Vue.defineComponent({
  name: 'CpConfirmDialog',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    dialog() { return this.s.confirmDialog || {}; },
    confirmBtnClass() {
      const tone = this.dialog.tone || 'danger';
      if (tone === 'warning') return 'btn btn-warning';
      if (tone === 'primary') return 'btn btn-primary';
      return 'btn btn-danger';
    },
  },
  methods: {
    cancel() { this.cp.resolveConfirm(false); },
    confirm() { this.cp.resolveConfirm(true); },
    onBackdropClick(ev) {
      if (ev.target === ev.currentTarget) this.cancel();
    },
  },
  mounted() {
    this._onKeyDown = (ev) => {
      if (ev.key !== 'Escape') return;
      if (!this.dialog || !this.dialog.open) return;
      ev.preventDefault();
      this.cancel();
    };
    document.addEventListener('keydown', this._onKeyDown);
  },
  unmounted() {
    if (this._onKeyDown) document.removeEventListener('keydown', this._onKeyDown);
  },
  template: `
    <div v-if="dialog.open" class="confirm-overlay" @click="onBackdropClick">
      <section class="confirm-dialog" role="dialog" aria-modal="true" aria-label="确认操作">
        <h3 class="confirm-title">{{ dialog.title || '请确认' }}</h3>
        <p class="confirm-message">{{ dialog.message || '' }}</p>
        <div class="confirm-actions">
          <button class="btn btn-outline" type="button" @click="cancel">
            {{ dialog.cancelText || '取消' }}
          </button>
          <button :class="confirmBtnClass" type="button" @click="confirm">
            {{ dialog.confirmText || '确认' }}
          </button>
        </div>
      </section>
    </div>
  `,
});
