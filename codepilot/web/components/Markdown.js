/* <cp-markdown :text="m.content">  → rendered GFM markdown.
 *
 * AI output (task content, chat bubbles, delivery records) is authored as
 * GitHub-flavoured markdown — rendering it as markdown gives us headings,
 * fenced code blocks with syntax highlighting, lists, tables and diff
 * callouts without any per-field formatting logic. Output is sanitised by
 * DOMPurify in CP.renderMarkdown before being injected. */
/* global Vue, CP */
CP.Components.Markdown = Vue.defineComponent({
  name: 'CpMarkdown',
  props: {
    text: { type: String, default: '' },
  },
  computed: {
    html() { return CP.renderOutput(this.text || ''); },
  },
  template: `<div class="md" v-html="html"></div>`,
});
