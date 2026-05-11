/* Project metrics card. */
/* global Vue, CP */
CP.Components.MetricsPanel = Vue.defineComponent({
  name: 'CpMetricsPanel',
  inject: ['cp'],
  computed: {
    currentProject() { return this.cp.currentProject; },
    metrics() { return this.cp.metrics; },
    aiStatus() { return this.cp.state.aiStatus || {}; },
    deepseekBalance() {
      const balance = (this.aiStatus.balances && this.aiStatus.balances.deepseek) || {};
      const infos = Array.isArray(balance.balance_infos) ? balance.balance_infos : [];
      return infos.map((item) => `${item.currency || ''} ${item.total_balance || '-'}`.trim()).filter(Boolean).join(' / ') || '-';
    },
    deepseekUsage() {
      const usage = (this.aiStatus.usage && this.aiStatus.usage.deepseek) || {};
      return {
        requests: Number(usage.requests || 0),
        total: Number(usage.total_tokens || 0),
        prompt: Number(usage.prompt_tokens || 0),
        completion: Number(usage.completion_tokens || 0),
        reasoning: Number(usage.reasoning_tokens || 0),
      };
    },
  },
  template: `
    <section class="project-status-card card">
      <div class="project-status-head">
        <div>
          <h3>项目状态</h3>
          <div class="project-status-summary">
            <span>活动摘要</span>
            <strong>{{ currentProject ? (currentProject.active_summary || '无') : '-' }}</strong>
          </div>
        </div>
        <button class="icon-btn" title="刷新余额和用量" @click="cp.loadAIStatus({ refresh: true })">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>
        </button>
      </div>
      <div class="project-status-grid">
        <div v-for="m in metrics" :key="m.label" class="project-status-tile" :class="m.tone">
          <div class="project-status-label">{{ m.label }}</div>
          <div class="project-status-value">{{ m.value }}</div>
        </div>
      </div>
      <div class="project-usage-panel">
        <div class="project-usage-head">
          <div>
            <div class="project-status-label">DeepSeek</div>
            <div class="project-usage-balance">{{ deepseekBalance }}</div>
          </div>
          <span class="muted tiny">余额 / 用量</span>
        </div>
        <div class="project-usage-grid">
          <span>请求 {{ deepseekUsage.requests }}</span>
          <span>总 tokens {{ deepseekUsage.total }}</span>
          <span>输入 {{ deepseekUsage.prompt }}</span>
          <span>输出 {{ deepseekUsage.completion }}</span>
          <span>思考 {{ deepseekUsage.reasoning }}</span>
        </div>
      </div>
    </section>
  `,
});
