/* <cp-chip tone="info" tiny>标签</cp-chip> */
/* global Vue, CP */
CP.Components.Chip = Vue.defineComponent({
  name: 'CpChip',
  props: {
    tone: { type: String, default: 'neutral' },
    tiny: Boolean,
  },
  template: `<span class="chip" :class="[tone, tiny ? 'tiny' : '']"><slot/></span>`,
});
