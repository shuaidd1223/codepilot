/* Quick input card. */
/* global Vue, CP */
CP.Components.GoalInput = Vue.defineComponent({
  name: 'CpGoalInput',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    goalPending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.GOAL_SUBMIT); },
  },
  methods: {
    submit() { this.cp.submitGoal(); },
    clearAnswer() { this.cp.state.answer = null; },
  },
  template: `
    <div>
      <div v-if="s.answer" class="answer-card">
        <svg class="answer-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
        <cp-markdown class="answer-body" :text="s.answer"></cp-markdown>
        <button class="icon-btn small" @click="clearAnswer">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>

      <div class="card pad" style="margin-top: 16px">
        <div class="row gap-sm wrap end">
          <div class="field grow">
            <label>快速输入</label>
            <input v-model="s.goalText" @keydown.enter.exact.prevent="submit" maxlength="4096" placeholder="输入问题、需求或命令… 由 AI 自动判断">
          </div>
          <div class="field w-28">
            <label>类型</label>
            <select v-model="s.goalCategory">
              <option value="auto">自动</option>
              <option value="question">问题</option>
              <option value="requirement">需求</option>
              <option value="command">命令</option>
            </select>
          </div>
          <button class="btn btn-primary" @click="submit" :disabled="goalPending">
            <span v-if="goalPending" class="spinner"></span>
            <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            提交
          </button>
        </div>
      </div>
    </div>
  `,
});
