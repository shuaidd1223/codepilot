/* <cp-live-log :events="liveEvents" tall follow>
 *
 * Renders progress_bus events (see codepilot/progress_bus.py) as a CLI-style
 * log: `[HH:MM:SS] [stage rX/Y] message`. Each event row gets a per-stage
 * accent colour (builder=blue, reviewer=magenta, error=red, …) so the eye
 * can scan the stream the same way it scans a Codex / Claude Code session.
 *
 * Messages containing ANSI escapes are still passed through ansi_up; messages
 * that look like a unified diff (rare but possible — review-tool output) go
 * through the `diff` highlighter. Everything else is HTML-escaped. */
/* global Vue, CP */
CP.Components.LiveLog = Vue.defineComponent({
  name: 'CpLiveLog',
  props: {
    events: { type: Array, default: () => [] },
    tall: Boolean,
    follow: { type: Boolean, default: true },
  },
  computed: {
    rendered() {
      return (this.events || []).map(ev => this._renderRow(ev));
    },
  },
  watch: {
    rendered() {
      if (!this.follow) return;
      this.$nextTick(() => {
        const el = this.$refs.pre;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  },
  methods: {
    _renderRow(ev) {
      const ts = (ev && ev.timestamp || '').slice(11, 19) || '--:--:--';
      const stage = (ev && ev.stage) || 'log';
      const level = (ev && ev.level) || 'info';
      const extra = (ev && ev.extra) || {};
      const round = extra.round && extra.round_total
        ? ` r${extra.round}/${extra.round_total}` : '';
      const msgRaw = (ev && ev.message) || '';
      const msgHtml = CP.renderCode(msgRaw, null);
      return {
        key: `${ts}-${stage}-${level}-${round}-${msgRaw.slice(0, 32)}`,
        ts,
        stage,
        level,
        round,
        msgHtml,
      };
    },
  },
  template: `
    <pre ref="pre" class="code live-log" :class="tall ? 'tall' : ''">
      <span v-for="row in rendered" :key="row.key"
            class="live-row"
            :class="['level-' + row.level, 'stage-' + row.stage]">
        <span class="live-ts">[{{ row.ts }}]</span>
        <span class="live-stage">[{{ row.stage }}{{ row.round }}]</span>
        <span class="live-msg" v-html="row.msgHtml"></span>
      </span>
    </pre>
  `,
});
