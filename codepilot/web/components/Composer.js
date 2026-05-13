/* Compose task/requirement form. */
/* global Vue, CP */
CP.Components.Composer = Vue.defineComponent({
  name: 'CpComposer',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    composerPending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.COMPOSER_SUBMIT); },
    isClarifyingRequirement() {
      return this.s.composerMode === 'requirement' && !!this.s.composerClarify;
    },
    modeHint() {
      const m = this.s.composerMode;
      if (m === 'requirement') return '推荐：写一段自然语言需求，由规划器拆成符合模板的任务。';
      if (m === 'task_ai') return '只写标题，后端调 AI 按 task-template 9 章节生成 content（生成失败 / 缺章节会被拒）。';
      return '完整任务：自己写标题 + 符合 task-template 9 章节的 content（缺章节会被拒）。';
    },
  },
  methods: {
    submit() { this.cp.submitComposer(); },
    cancelClarify() { this.cp.cancelComposerClarify(); },
    updateClarifyAnswers(nextAnswers) {
      if (this.s.composerClarify) this.s.composerClarify.answers = nextAnswers;
    },
  },
  template: `
    <section class="card">
      <div class="card-head">
        <h3>发起工作</h3>
        <p class="muted">3 选 1：自然语言需求 / 完整任务 / 仅标题（AI 补全）</p>
      </div>
      <form class="form" @submit.prevent="submit">
        <div v-if="isClarifyingRequirement" class="clarify-panel">
          <div class="clarify-head">继续完善这次需求规划</div>
          <div v-if="s.composerClarify.original_title" class="muted tiny" style="margin-bottom:8px">
            原始需求：{{ s.composerClarify.original_title }}
          </div>
          <cp-clarify-fields
            :questions="s.composerClarify.questions"
            :answers="s.composerClarify.answers"
            @update:answers="updateClarifyAnswers"
          ></cp-clarify-fields>
        </div>
        <div class="grid grid-2 gap-sm">
          <div class="field">
            <label>模式</label>
            <select v-model="s.composerMode">
              <option value="requirement">自然语言需求（走规划器）</option>
              <option value="task">完整任务（自己提供 content）</option>
              <option value="task_ai">仅标题，AI 补全 content</option>
            </select>
            <div class="field-hint tiny muted">{{ modeHint }}</div>
          </div>
          <div class="field">
            <label>优先级</label>
            <select v-model="s.composer.priority">
              <option>P0</option><option>P1</option><option>P2</option><option>P3</option>
            </select>
          </div>
          <div v-if="!isClarifyingRequirement" class="field full">
            <label>标题</label>
            <textarea v-model="s.composer.title" rows="2" placeholder="例如：把失败任务的原因直接显示在 UI 里，并一键重试"></textarea>
          </div>
          <div class="field full" v-if="s.composerMode !== 'task_ai' && !isClarifyingRequirement">
            <label>{{ s.composerMode === 'task' ? 'content（必须符合 task-template 9 章节）' : '补充说明' }}</label>
            <textarea v-model="s.composer.content" rows="6" :placeholder="s.composerMode === 'task' ? '## Task Goal\\n...\\n\\n## In Scope\\n- ...\\n\\n## Acceptance Criteria\\n...（参见 ai template --format md）' : '可选：范围、约束、验收标准'"></textarea>
          </div>
          <div class="field">
            <label>任务智能体</label>
            <select v-model="s.composer.agent">
              <option value="auto">跟随项目默认</option>
              <option value="dual">dual</option>
              <option value="codex">codex</option>
              <option value="claude">claude</option>
            </select>
          </div>
          <div class="field" v-if="s.composerMode === 'requirement'">
            <label>规划智能体</label>
            <select v-model="s.composer.planner">
              <option value="codex">codex</option>
              <option value="claude">claude</option>
            </select>
          </div>
        </div>
        <div class="row between">
          <label class="checkbox">
            <input type="checkbox" v-model="s.composer.execute" :disabled="s.composerMode !== 'requirement'">
            提交后立即执行
          </label>
          <div class="row gap-sm end">
            <button v-if="isClarifyingRequirement" type="button" class="btn btn-outline" @click="cancelClarify">
              取消本次规划
            </button>
            <button type="submit" class="btn btn-primary" :disabled="composerPending">
              <span v-if="composerPending" class="spinner"></span>
              {{ isClarifyingRequirement ? '继续规划' : '提交到当前项目' }}
            </button>
          </div>
        </div>
      </form>
    </section>
  `,
});
