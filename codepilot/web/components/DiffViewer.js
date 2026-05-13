/* <cp-diff-viewer :block="diffBlock"></cp-diff-viewer>
 *
 * Plugin-based diff renderer powered by diff2html.
 * Falls back to plain unified-diff text when plugin is unavailable.
 */
/* global Vue, CP */

CP.Components.DiffViewer = Vue.defineComponent({
  name: 'CpDiffViewer',
  props: {
    block: { type: Object, required: true },
    height: { type: [Number, String], default: '' },
  },
  data() {
    return {
      error: '',
    };
  },
  computed: {
    lines() {
      return (this.block && this.block.lines) || [];
    },
    fileName() {
      return String((this.block && this.block.file) || '');
    },
    heightPx() {
      if (typeof this.height === 'number' && Number.isFinite(this.height)) return this.height;
      if (typeof this.height === 'string' && this.height.trim()) return this.height.trim();
      const rows = this.lines.length || 0;
      return Math.max(180, Math.min(560, 120 + (rows * 9)));
    },
    unifiedText() {
      if (!this.lines.length) return '';
      return this.lines.map(r => (r && r.text) || '').join('\n');
    },
    fallbackText() {
      if (!this.lines.length) return '';
      return this.lines.map(r => (r && r.text) || '').join('\n');
    },
  },
  watch: {
    unifiedText() { this.$nextTick(() => this._renderPlugin()); },
    fileName() { this.$nextTick(() => this._renderPlugin()); },
  },
  mounted() {
    this._renderPlugin();
  },
  methods: {
    _renderPlugin() {
      const host = this.$refs.host;
      if (!host) return;
      this.error = '';
      const text = this.unifiedText;
      if (!text) {
        host.innerHTML = '';
        return;
      }
      if (!window.Diff2Html || typeof window.Diff2Html.html !== 'function') {
        this.error = 'diff2html 插件未就绪，已回退文本视图';
        host.innerHTML = '';
        return;
      }
      try {
        const html = window.Diff2Html.html(text, {
          drawFileList: false,
          matching: 'none',
          outputFormat: 'line-by-line',
          renderNothingWhenEmpty: false,
        });
        host.innerHTML = html || '';
      } catch (_e) {
        this.error = 'diff2html 渲染失败，已回退文本视图';
        host.innerHTML = '';
      }
    },
  },
  template: `
    <div class="cp-diffv" :style="{ height: (typeof heightPx === 'number' ? (heightPx + 'px') : heightPx) }">
      <div v-if="error" class="cp-diffv-fallback">
        <div class="cp-diffv-note">{{ error }}</div>
        <pre class="cp-diffv-pre">{{ fallbackText }}</pre>
      </div>
      <div v-else ref="host" class="cp-diffv-host"></div>
    </div>
  `,
});
