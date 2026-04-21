/* <cp-code-block :text="..." lang="diff" tall> — syntax-highlighted <pre>.
 *
 * Centralises the three rendering modes the UI needs:
 *   - ANSI escape sequences (builder/reviewer stdout) → ansi_up palette.
 *   - Unified diffs → hljs `diff` grammar with our `+/-` row tint.
 *   - Anything else → hljs auto-detect (capped so huge logs stay fast).
 *
 * Consumers just pass raw text; the component does escaping and memoises the
 * rendered HTML so re-renders (e.g. SSE event arrives, parent re-renders)
 * don't re-run hljs unnecessarily. */
/* global Vue, CP */
CP.Components.CodeBlock = Vue.defineComponent({
  name: 'CpCodeBlock',
  props: {
    text: { type: String, default: '' },
    lang: { type: String, default: '' },
    tall: Boolean,
    /* When true the block auto-scrolls to the bottom on text growth — used by
     * live log panes so new lines are always visible. */
    follow: Boolean,
  },
  computed: {
    html() {
      return CP.renderCode(this.text || '', this.lang || null);
    },
    languageClass() {
      if (this.lang) return `language-${this.lang}`;
      if (CP._DIFF_RE.test(this.text || '')) return 'language-diff';
      return '';
    },
  },
  watch: {
    text() {
      if (!this.follow) return;
      this.$nextTick(() => {
        const el = this.$refs.pre;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  },
  template: `
    <pre ref="pre" class="code hljs" :class="[languageClass, tall ? 'tall' : '']"
         v-html="html"></pre>
  `,
});
