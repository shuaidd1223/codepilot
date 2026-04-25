/* Structured clarification questionnaire fields. */
/* global Vue, CP */
CP.Components.ClarifyFields = Vue.defineComponent({
  name: 'CpClarifyFields',
  props: {
    questions: { type: Array, default: () => [] },
    answers: { type: Object, default: () => ({}) },
  },
  emits: ['update:answers'],
  data() {
    return {
      groupPrefix: `clarify-${Math.random().toString(36).slice(2, 10)}`,
    };
  },
  computed: {
    normalizedQuestions() { return CP.normalizeClarifyQuestions(this.questions); },
  },
  methods: {
    stateFor(question) {
      const current = this.answers && typeof this.answers === 'object'
        ? this.answers[question.id] || {}
        : {};
      return {
        selectedOptionIds: Array.isArray(current.selectedOptionIds)
          ? current.selectedOptionIds.slice()
          : [],
        text: typeof current.text === 'string' ? current.text : '',
      };
    },
    commitState(question, nextState) {
      const answers = this.answers && typeof this.answers === 'object'
        ? { ...this.answers }
        : {};
      answers[question.id] = {
        selectedOptionIds: Array.isArray(nextState.selectedOptionIds)
          ? nextState.selectedOptionIds.slice()
          : [],
        text: typeof nextState.text === 'string' ? nextState.text : '',
      };
      this.$emit('update:answers', answers);
    },
    isChecked(question, optionId) {
      return this.stateFor(question).selectedOptionIds.includes(optionId);
    },
    radioName(question) {
      return `${this.groupPrefix}-${question.id}`;
    },
    setSingle(question, optionId) {
      this.commitState(question, {
        selectedOptionIds: optionId ? [optionId] : [],
        text: '',
      });
    },
    toggleMulti(question, optionId, checked) {
      const state = this.stateFor(question);
      const current = new Set(state.selectedOptionIds);
      if (checked) current.add(optionId);
      else current.delete(optionId);
      this.commitState(question, {
        selectedOptionIds: Array.from(current),
        text: state.text,
      });
    },
    updateFreeText(question, value) {
      const state = this.stateFor(question);
      const text = String(value == null ? '' : value);
      this.commitState(question, {
        selectedOptionIds: question.type === 'single' && question.allow_free_text && text.trim()
          ? []
          : state.selectedOptionIds,
        text,
      });
    },
    updateText(question, value) {
      this.commitState(question, {
        selectedOptionIds: [],
        text: String(value == null ? '' : value),
      });
    },
  },
  template: `
    <div class="clarify-form">
      <div v-for="q in normalizedQuestions" :key="q.id" class="clarify-question">
        <div class="clarify-question-head">
          <span class="clarify-question-text">{{ q.text }}</span>
          <span class="clarify-question-type">{{ q.type }}</span>
        </div>

        <div v-if="q.type === 'text'" class="field">
          <textarea
            :value="stateFor(q).text"
            rows="2"
            placeholder="输入你的回答"
            @input="updateText(q, $event.target.value)"
          ></textarea>
        </div>

        <div v-else class="clarify-option-list">
          <label v-for="opt in q.options" :key="opt.id" class="clarify-option">
            <input
              v-if="q.type === 'single'"
              type="radio"
              :name="radioName(q)"
              :checked="isChecked(q, opt.id)"
              @change="setSingle(q, opt.id)"
            >
            <input
              v-else
              type="checkbox"
              :checked="isChecked(q, opt.id)"
              @change="toggleMulti(q, opt.id, $event.target.checked)"
            >
            <span>{{ opt.label }}</span>
          </label>
          <div v-if="q.allow_free_text" class="field clarify-free-text">
            <label>其他 / 手动输入</label>
            <input
              :value="stateFor(q).text"
              type="text"
              placeholder="没有合适选项时可直接输入"
              @input="updateFreeText(q, $event.target.value)"
            >
          </div>
        </div>
      </div>
    </div>
  `,
});
