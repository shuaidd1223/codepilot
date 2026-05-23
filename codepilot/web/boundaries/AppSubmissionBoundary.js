/* Submission/project-actions boundary extracted from AppStateBoundary.
 * Owns goal/composer submits, batch task import, and project/service actions. */
/* global CP */

window.CP = window.CP || {};

CP.createAppSubmissionBoundary = (options = {}) => {
  const state = options.state;
  const pushToast = options.pushToast || (() => {});
  const runScopedAction = options.runScopedAction || (async (_key, runner) => runner());
  const ACTION_KEYS = options.ACTION_KEYS || {};
  const loadDashboard = options.loadDashboard || (async () => {});
  const selectTask = options.selectTask || (() => {});
  const selectProject = options.selectProject || (() => {});
  const setNav = options.setNav || (() => {});
  const confirmDialog = options.confirmDialog || (async () => false);
  const deleteProjectDraft = options.deleteProjectDraft || (() => {});

  async function submitGoal() {
    if (!state.nav.project) {
      pushToast('先选择一个项目', 'error');
      return;
    }
    const text = state.goalText.trim();
    if (!text) {
      pushToast('输入不能为空', 'error');
      return;
    }
    await runScopedAction(ACTION_KEYS.GOAL_SUBMIT, async () => {
      state.answer = null;
      try {
        const payload = {
          project: state.nav.project,
          text,
          category: state.goalCategory,
        };
        const out = await CP.api.post('/api/goal', payload);
        if (out.intent === 'question' || out.intent === 'command') {
          state.answer = out.message || '完成';
        } else {
          pushToast(out.message || '提交成功', 'success');
          await loadDashboard();
        }
        state.goalText = '';
      } catch (err) {
        pushToast(err.message, 'error');
      }
    });
  }

  async function submitComposer() {
    if (!state.nav.project) {
      pushToast('先选择一个项目', 'error');
      return;
    }
    const title = state.composer.title.trim();
    if (!title) {
      pushToast('标题不能为空', 'error');
      return;
    }
    await runScopedAction(ACTION_KEYS.COMPOSER_SUBMIT, async () => {
      const payload = {
        project: state.nav.project,
        title,
        content: state.composer.content,
        priority: state.composer.priority,
        agent: state.composer.agent,
        planner: state.composer.planner,
        execute: state.composer.execute,
      };
      try {
        let out;
        if (state.composerMode === 'task' || state.composerMode === 'task_ai') {
          payload.mode = state.composerMode === 'task_ai' ? 'ai_complete' : 'full';
          if (state.composerMode === 'task_ai') {
            payload.content = '';
          }
          out = await CP.api.post('/api/tasks', payload);
          if (out.task) selectTask(state.nav.project, out.task.id);
          state.composer.content = '';
        } else {
          out = await CP.api.post('/api/requirements', payload);
        }
        state.composer.title = '';
        pushToast(out.message || '提交成功', 'success');
        await loadDashboard();
      } catch (err) {
        pushToast(err.message, 'error');
      }
    });
  }

  async function loadTaskTemplateSchema({ force = false } = {}) {
    if (state.taskTemplateLoading) return state.taskTemplateSchema;
    if (!force && state.taskTemplateSchema) return state.taskTemplateSchema;
    state.taskTemplateLoading = true;
    state.taskTemplateError = '';
    try {
      const out = await CP.api.get('/api/task-template');
      state.taskTemplateSchema = out.schema || null;
      return state.taskTemplateSchema;
    } catch (err) {
      state.taskTemplateError = err.message || '模板 schema 加载失败';
      return null;
    } finally {
      state.taskTemplateLoading = false;
    }
  }

  async function submitTaskBatch(validation = null) {
    if (!state.nav.project) {
      pushToast('先选择一个项目', 'error');
      return;
    }
    const raw = String((state.batchComposer && state.batchComposer.raw) || '').trim();
    if (!raw) {
      pushToast('先粘贴批量任务 JSON', 'error');
      return;
    }

    const resolvedValidation = validation || CP.validateTaskBatchImport(raw, state.taskTemplateSchema);
    if (!resolvedValidation.valid) {
      const firstInvalidItem = (resolvedValidation.items || []).find((item) => !item.ok && item.errors && item.errors.length);
      const firstError = resolvedValidation.parseError
        || (resolvedValidation.globalErrors && resolvedValidation.globalErrors[0])
        || (firstInvalidItem && firstInvalidItem.errors && firstInvalidItem.errors[0])
        || '当前批量任务不符合模板 schema。';
      pushToast(firstError, 'error');
      return;
    }

    await runScopedAction(ACTION_KEYS.TASK_BATCH_IMPORT, async () => {
      try {
        const out = await CP.api.post('/api/tasks/import', {
          project: state.nav.project,
          items: resolvedValidation.items.map((item) => item.raw),
        });
        state.batchComposer.raw = '';
        pushToast(out.message || '批量导入成功', 'success');
        await loadDashboard();
        if (out.tasks && out.tasks.length) selectTask(state.nav.project, out.tasks[0].id);
      } catch (err) {
        pushToast(err.message, 'error');
      }
    });
  }

  function toggleProjectForm(open = null) {
    state.projectForm.open = open == null ? !state.projectForm.open : !!open;
  }

  async function submitProject() {
    const path = state.projectForm.path.trim();
    if (!path) {
      pushToast('工作目录不能为空', 'error');
      return;
    }
    state.projectSubmitting = true;
    try {
      const out = await CP.api.post('/api/projects', {
        path,
        name: state.projectForm.name.trim(),
        no_config: !!state.projectForm.noConfig,
      });
      state.projectForm = { open: false, path: '', name: '', noConfig: false };
      await loadDashboard();
      if (out.project && out.project.name) selectProject(out.project.name);
      pushToast(out.message || '项目已注册', 'success');
    } catch (err) {
      pushToast(err.message, 'error');
    } finally {
      state.projectSubmitting = false;
    }
  }

  async function deleteProject(name) {
    if (!name) return;
    const ok = await confirmDialog({
      title: '删除项目',
      message: `确定要删除项目 "${name}" 吗？\n工作目录不会被删除。`,
      confirmText: '删除',
      cancelText: '取消',
      tone: 'danger',
    });
    if (!ok) return;
    state.deletingProject = name;
    try {
      const out = await CP.api.del(`/api/projects/${encodeURIComponent(name)}`);
      if (state.nav.project === name) {
        setNav({ project: null, view: 'overview', id: null });
        state.taskDetail = null;
        state.sessionDetail = null;
        state.sessionMessages = [];
      }
      deleteProjectDraft(name);
      await loadDashboard();
      pushToast(out.message || '项目已删除', 'success');
    } catch (err) {
      pushToast(err.message, 'error');
    } finally {
      state.deletingProject = '';
    }
  }

  async function projectService(service, action) {
    if (!state.nav.project) {
      pushToast('先选择一个项目', 'error');
      return;
    }
    const key = `${service}:${action}`;
    state.servicePending = key;
    try {
      const out = await CP.api.post(
        `/api/projects/${encodeURIComponent(state.nav.project)}/${service}/${action}`,
        {},
      );
      pushToast(out.message || '操作完成', 'success');
      await loadDashboard();
    } catch (err) {
      pushToast(err.message, 'error');
    } finally {
      state.servicePending = '';
    }
  }

  async function jobAction(job, action) {
    if (!job || !job.id || !action) return;
    if (action === 'cancel') {
      const ok = await confirmDialog({
        title: '停止需求',
        message: `确定要停止需求 #${job.id} 吗？\n如果底层 AI 调用正在进行，会在本轮调用返回后停止后续创建和执行。`,
        confirmText: '停止',
        cancelText: '取消',
        tone: 'danger',
      });
      if (!ok) return;
    }
    const key = `${ACTION_KEYS.JOB_ACTION}:${job.id}:${action}`;
    await runScopedAction(key, async () => {
      try {
        const out = await CP.api.post(`/api/jobs/${job.id}/${action}`, {});
        pushToast(out.message || '操作完成', action === 'cancel' ? 'warning' : 'success');
        await loadDashboard();
      } catch (err) {
        pushToast(err.message, 'error');
      }
    });
  }

  return {
    submitGoal,
    submitComposer,
    loadTaskTemplateSchema,
    submitTaskBatch,
    toggleProjectForm,
    submitProject,
    deleteProject,
    projectService,
    jobAction,
  };
};
