/* <cp-live-log :events="liveEvents" tall follow>
 *
 * Renders progress_bus events grouped by phase so heartbeat-heavy streams can
 * be collapsed into stage-sized sections instead of a flat endless list.
 * Phase boundaries come from SSE event.type = phase_start / phase_end / error;
 * older untyped events still fall back to stage-based grouping. */
/* global Vue, CP */
CP.Components.LiveLog = Vue.defineComponent({
  name: 'CpLiveLog',
  props: {
    events: { type: Array, default: () => [] },
    tall: Boolean,
    follow: { type: Boolean, default: true },
  },
  data() {
    return {
      groupOpen: {},
    };
  },
  computed: {
    phaseGroups() {
      const groups = [];
      let current = null;
      for (let i = 0; i < (this.events || []).length; i += 1) {
        const ev = this.events[i];
        const row = this._renderRow(ev, i);
        const type = row.type;
        const sameStage = !!(current && current.stage === row.stage);
        const shouldStartNew = type === 'phase_start' || !current || current.closed || !sameStage;
        if (shouldStartNew) {
          current = this._createGroup(row, ev, i);
          groups.push(current);
        }
        this._appendRow(current, row);
      }
      return groups.map((group, index) => ({
        ...group,
        defaultOpen: group.statusType === 'error' || !group.closed || index === groups.length - 1,
        statusLabel: this._groupStatusLabel(group),
        preview: this._groupPreview(group),
      }));
    },
  },
  watch: {
    phaseGroups: {
      immediate: true,
      handler(groups) {
        this._syncGroupOpen(groups);
        if (!this.follow) return;
        this.$nextTick(() => {
          const el = this.$refs.pre;
          if (el) el.scrollTop = el.scrollHeight;
        });
      },
    },
  },
  methods: {
    _renderRow(ev, index) {
      const ts = (ev && ev.timestamp || '').slice(11, 19) || '--:--:--';
      const stage = String((ev && ev.stage) || 'log');
      const level = String((ev && ev.level) || 'info');
      const type = String((ev && ev.type) || '');
      const extra = (ev && ev.extra) || {};
      const stageBase = String(extra.phase_kind || stage).trim().split(/\s+/)[0].toLowerCase() || 'log';
      const hasRound = extra.round && extra.round_total;
      const roundToken = hasRound ? `r${extra.round}/${extra.round_total}` : '';
      const round = roundToken && !stage.includes(roundToken) ? ` ${roundToken}` : '';
      const msgRaw = String((ev && ev.message) || '');
      const msgHtml = msgRaw ? CP.renderCode(msgRaw, null) : '';
      return {
        key: `${(ev && ev.id) || index}-${stageBase}-${level}`,
        id: (ev && ev.id) || 0,
        ts,
        stage,
        stageBase,
        level,
        type,
        round,
        msgRaw,
        msgText: msgRaw.trim(),
        msgHtml,
      };
    },
    _createGroup(row, ev, index) {
      return {
        key: `phase-${(ev && ev.id) || index}-${row.stageBase}`,
        stage: row.stage,
        stageBase: row.stageBase,
        rows: [],
        eventCount: 0,
        heartbeatCount: 0,
        closed: false,
        statusType: '',
        lastMessage: row.msgText,
      };
    },
    _appendRow(group, row) {
      group.rows.push(row);
      group.eventCount += 1;
      if (row.type === 'heartbeat') group.heartbeatCount += 1;
      if (row.msgText) group.lastMessage = row.msgText;
      if (row.type === 'error') {
        group.closed = true;
        group.statusType = 'error';
      } else if (row.type === 'phase_end' && group.statusType !== 'error') {
        group.closed = true;
        group.statusType = 'phase_end';
      }
    },
    _groupStatusLabel(group) {
      if (group.statusType === 'error') return '错误';
      if (!group.closed) return '进行中';
      return '完成';
    },
    _groupPreview(group) {
      if (group.lastMessage) return group.lastMessage;
      if (group.heartbeatCount) return `收到 ${group.heartbeatCount} 条 heartbeat`;
      return `${group.eventCount} 条事件`;
    },
    _syncGroupOpen(groups) {
      const next = {};
      const current = this.groupOpen || {};
      for (const group of (groups || [])) {
        if (Object.prototype.hasOwnProperty.call(current, group.key)) {
          next[group.key] = !!current[group.key];
        } else {
          next[group.key] = !!group.defaultOpen;
        }
      }
      this.groupOpen = next;
    },
    isGroupOpen(group) {
      if (Object.prototype.hasOwnProperty.call(this.groupOpen || {}, group.key)) {
        return !!this.groupOpen[group.key];
      }
      return !!group.defaultOpen;
    },
    onGroupToggle(group, ev) {
      const open = !!(ev && ev.target && ev.target.open);
      if (this.groupOpen && this.groupOpen[group.key] === open) return;
      this.groupOpen = {
        ...(this.groupOpen || {}),
        [group.key]: open,
      };
    },
  },
  template: `
    <div ref="pre" class="live-log-panel" :class="tall ? 'tall' : ''">
      <div v-if="!phaseGroups.length" class="live-log-empty">暂无实时事件</div>
      <template v-else>
        <details v-for="group in phaseGroups"
                 :key="group.key"
                 class="live-log-group"
                 :class="['stage-' + group.stageBase, 'status-' + (group.statusType || (group.closed ? 'phase_end' : 'active'))]"
                 :open="isGroupOpen(group)"
                 @toggle="onGroupToggle(group, $event)">
          <summary class="live-log-summary">
            <span class="live-log-stage">{{ group.stage }}</span>
            <span class="live-log-badge">{{ group.statusLabel }}</span>
            <span class="live-log-meta">{{ group.eventCount }} 条</span>
            <span v-if="group.heartbeatCount" class="live-log-meta">heartbeat {{ group.heartbeatCount }}</span>
            <span class="live-log-preview">{{ group.preview }}</span>
          </summary>
          <div class="live-log-body">
            <div v-for="row in group.rows"
                 :key="row.key"
                 class="live-row"
                 :class="['level-' + row.level, 'stage-' + row.stageBase, row.type ? 'type-' + row.type : '']">
              <span class="live-ts">[{{ row.ts }}]</span>
              <span class="live-stage">[{{ row.stage }}{{ row.round }}]</span>
              <span class="live-msg" v-html="row.msgHtml"></span>
            </div>
          </div>
        </details>
      </template>
    </div>
  `,
});
