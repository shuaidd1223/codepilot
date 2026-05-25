/* Submission/project-actions boundary extracted from AppStateBoundary.
 * Owns project, service, and job actions. */
/* global CP */

window.CP = window.CP || {};

CP.createAppSubmissionBoundary = (options = {}) => {
  const state = options.state;
  const pushToast = options.pushToast || (() => {});
  const runScopedAction = options.runScopedAction || (async (_key, runner) => runner());
  const ACTION_KEYS = options.ACTION_KEYS || {};
  const loadDashboard = options.loadDashboard || (async () => {});
  const selectProject = options.selectProject || (() => {});
  const setNav = options.setNav || (() => {});
  const confirmDialog = options.confirmDialog || (async () => false);
  const deleteProjectDraft = options.deleteProjectDraft || (() => {});

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
    toggleProjectForm,
    submitProject,
    deleteProject,
    projectService,
    jobAction,
  };
};
