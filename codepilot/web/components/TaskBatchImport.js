/* Bulk import tasks via JSON with live task-template validation. */
/* global Vue, CP */
CP.Components.TaskBatchImport = Vue.defineComponent({
  name: 'CpTaskBatchImport',
  inject: ['cp'],
  computed: {
    s() { return this.cp.state; },
    schema() { return this.s.taskTemplateSchema; },
    batchPending() { return this.cp.isActionPending(this.cp.ACTION_KEYS.TASK_BATCH_IMPORT); },
    validation() { return CP.validateTaskBatchImport(this.s.batchComposer.raw, this.schema); },
    requiredHeadings() {
      const validation = (this.schema && this.schema.validation) || {};
      return (validation.required_headings || []).map(item => item.label).filter(Boolean);
    },
    templateHeadings() {
      const markdown = String((this.schema && this.schema.template_markdown) || '');
      const headings = [];
      const rx = /^##\s+(.+?)\s*$/gm;
      let match;
      while ((match = rx.exec(markdown)) !== null) {
        const label = String(match[1] || '').trim();
        if (label) headings.push(label);
      }
      return headings.length ? headings : this.requiredHeadings;
    },
    placeholderTokens() {
      const validation = (this.schema && this.schema.validation) || {};
      return (validation.placeholder_tokens || []).slice(0, 8);
    },
    summaryTone() {
      if (this.validation.valid) return 'success';
      if (this.validation.total || this.validation.parseError || this.validation.globalErrors.length) return 'warning';
      if (this.s.taskTemplateError) return 'danger';
      return 'neutral';
    },
    summaryText() {
      if (this.s.taskTemplateLoading) return '正在加载模板 schema...';
      if (this.s.taskTemplateError) return this.s.taskTemplateError;
      if (this.validation.parseError) return this.validation.parseError;
      if (this.validation.globalErrors.length) return this.validation.globalErrors[0];
      if (!this.validation.total) return '粘贴 tasks.json 后会按模板 schema 实时校验。';
      if (this.validation.valid) return `共 ${this.validation.total} 条，全部通过校验。`;
      return `共 ${this.validation.total} 条，${this.validation.invalidCount} 条待修正。`;
    },
  },
  mounted() {
    this.cp.loadTaskTemplateSchema();
  },
  methods: {
    submit() { this.cp.submitTaskBatch(this.validation); },
    applyExample() {
      const batch = (this.schema && this.schema.batch_import) || {};
      const example = batch.example || [];
      this.s.batchComposer.raw = JSON.stringify(example, null, 2);
    },
    clearRaw() {
      this.s.batchComposer.raw = '';
    },
  },
  template: `
    <section class="card">
      <div class="card-head row between gap-sm">
        <div class="min-w grow">
          <h3>批量添加任务</h3>
          <p class="muted">粘贴符合 task template 的 tasks.json，前端按 schema 实时校验，服务端再兜底校验一次。</p>
        </div>
        <div class="row gap-xs">
          <cp-chip :tone="summaryTone">{{ validation.valid ? '校验通过' : '批量导入' }}</cp-chip>
          <button type="button" class="btn btn-outline btn-sm" @click="applyExample" :disabled="!schema || batchPending">
            填充示例
          </button>
        </div>
      </div>
      <form class="form" @submit.prevent="submit">
        <div class="field">
          <label>tasks.json</label>
          <textarea
            v-model="s.batchComposer.raw"
            rows="14"
            placeholder='[{"title":"示例任务","content":"# 示例任务\\n\\n## Task Goal\\n..."}]'></textarea>
          <div class="field-hint">
            JSON 数组；每项至少包含 <code>title</code> 和符合模板的 <code>content</code>。
          </div>
        </div>

        <div class="batch-import-grid">
          <div class="batch-import-panel">
            <div class="batch-import-panel-head">实时校验</div>
            <div class="tiny muted">{{ summaryText }}</div>

            <div v-if="validation.parseError" class="batch-import-list mt-sm">
              <div class="batch-import-row danger">{{ validation.parseError }}</div>
            </div>
            <div v-else-if="validation.globalErrors.length" class="batch-import-list mt-sm">
              <div v-for="(err, idx) in validation.globalErrors" :key="'global-' + idx" class="batch-import-row danger">
                {{ err }}
              </div>
            </div>
            <div v-else-if="validation.total" class="batch-import-list mt-sm">
              <div
                v-for="item in validation.items"
                :key="item.index"
                class="batch-import-row"
                :class="item.ok ? 'success' : 'warning'">
                <div class="row between gap-sm">
                  <div class="batch-import-row-title">#{{ item.index }} {{ item.title || '未命名任务' }}</div>
                  <cp-chip tiny :tone="item.ok ? 'success' : 'warning'">{{ item.ok ? '通过' : '待修正' }}</cp-chip>
                </div>
                <div v-if="item.errors.length" class="batch-import-errors tiny">
                  <div v-for="(err, idx) in item.errors" :key="'err-' + item.index + '-' + idx">{{ err }}</div>
                </div>
                <div v-else class="tiny muted">章节完整，未发现模板占位符残留。</div>
              </div>
            </div>
          </div>

          <div class="batch-import-panel">
            <div class="batch-import-panel-head">完整模板结构</div>
            <div class="tiny muted">以下章节来自真实 task-template，每条任务的 <code>content</code> 应保持这些结构。</div>
            <div class="chip-row mt-sm">
              <cp-chip v-for="label in templateHeadings" :key="label">{{ label }}</cp-chip>
            </div>

            <div v-if="placeholderTokens.length" class="batch-import-notes">
              <div class="tiny muted">如果正文里仍出现这些占位符，说明模板还没填完：</div>
              <div class="chip-row mt-xs">
                <cp-chip v-for="token in placeholderTokens" :key="token" tone="warning">{{ token }}</cp-chip>
              </div>
            </div>

            <div class="batch-import-actions">
              <button type="button" class="btn btn-outline btn-sm" @click="clearRaw" :disabled="!s.batchComposer.raw || batchPending">
                清空
              </button>
            </div>
          </div>
        </div>

        <div class="row between gap-sm">
          <div class="tiny muted">校验通过后才允许提交，避免把只有标题或缺章节的占位任务写进 backlog。</div>
          <button type="submit" class="btn btn-primary" :disabled="batchPending || !validation.valid">
            <span v-if="batchPending" class="spinner"></span>
            批量导入到当前项目
          </button>
        </div>
      </form>
    </section>
  `,
});
