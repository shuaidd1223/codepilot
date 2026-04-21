/* Compose task/requirement form. */
/* global Vue, CP */
CP.Components.Composer = Vue.defineComponent({
  name: 'CpComposer',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
  },
  methods: {
    submit() { this.cp.submitComposer(); },
  },
  template: `
    <section class="card">
      <div class="card-head">
        <h3>发起工作</h3>
        <p class="muted">提交自然语言需求，或直接建任务</p>
      </div>
      <form class="form" @submit.prevent="submit">
        <div v-if="s.composerClarify && s.composerMode === 'requirement'" class="clarify-panel">
          <div class="clarify-head">需要澄清几个点</div>
          <ol>
            <li v-for="q in s.composerClarify.questions" :key="q">{{ q }}</li>
          </ol>
        </div>
        <div class="grid grid-2 gap-sm">
          <div class="field">
            <label>模式</label>
            <select v-model="s.composerMode">
              <option value="requirement">自然语言需求</option>
              <option value="task">直接建任务</option>
            </select>
          </div>
          <div class="field">
            <label>优先级</label>
            <select v-model="s.composer.priority">
              <option>P0</option><option>P1</option><option>P2</option><option>P3</option>
            </select>
          </div>
          <div class="field full">
            <label>{{ s.composerClarify && s.composerMode === 'requirement' ? '补充回答' : '标题' }}</label>
            <textarea v-model="s.composer.title" rows="2" :placeholder="s.composerClarify && s.composerMode === 'requirement' ? '回答上面的问题，可以一次性写完' : '例如：把失败任务的原因直接显示在 UI 里，并一键重试'"></textarea>
          </div>
          <div class="field full">
            <label>补充说明</label>
            <textarea v-model="s.composer.content" rows="3" placeholder="可选：范围、约束、验收标准"></textarea>
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
            <input type="checkbox" v-model="s.composer.execute" :disabled="s.composerMode === 'task'">
            提交后立即执行
          </label>
          <button type="submit" class="btn btn-primary" :disabled="s.sending">
            <span v-if="s.sending" class="spinner"></span>
            提交到当前项目
          </button>
        </div>
      </form>
    </section>
  `,
});
