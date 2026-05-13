/* Shared state-transition helpers extracted from app.js.
 * Keeps navigation/state mutations centralized and reusable. */
/* global CP */

window.CP = window.CP || {};
CP.StateBoundary = CP.StateBoundary || {};

CP.StateBoundary.CATEGORY_VIEWS = Object.freeze(['sessions', 'tasks', 'jobs']);

CP.StateBoundary.navToHash = (nav) => {
  if (!nav || !nav.project) return '';
  const project = encodeURIComponent(nav.project);
  if (nav.view === 'task' && nav.id) return `#/p/${project}/task/${nav.id}`;
  if (nav.view === 'session' && nav.id) return `#/p/${project}/session/${nav.id}`;
  if (nav.view === 'job' && nav.id) return `#/p/${project}/job/${nav.id}`;
  if (nav.view === 'tasks') return `#/p/${project}/tasks`;
  if (nav.view === 'sessions') return `#/p/${project}/sessions`;
  if (nav.view === 'jobs') return `#/p/${project}/jobs`;
  return `#/p/${project}`;
};

CP.StateBoundary.navFromHash = (hash) => {
  if (!hash || hash.length < 2) return null;
  const raw = hash.replace(/^#\/?/, '');
  const parts = raw.split('/').filter(Boolean);
  if (parts.length < 2 || parts[0] !== 'p') return null;
  let project = '';
  try {
    project = decodeURIComponent(parts[1]);
  } catch (_e) {
    return null;
  }
  if (!project) return null;
  if (parts.length === 2) return { project, view: 'overview', id: null };
  const view = parts[2];
  if (['task', 'session', 'job'].includes(view)) {
    const idNum = Number(parts[3]);
    if (!Number.isFinite(idNum)) return { project, view: 'overview', id: null };
    return { project, view, id: idNum };
  }
  if (['tasks', 'sessions', 'jobs', 'overview'].includes(view)) {
    return { project, view, id: null };
  }
  return { project, view: 'overview', id: null };
};

CP.StateBoundary.categoryKey = (project, view) => `${project}/${view}`;

CP.StateBoundary.openProjectCategory = (state, project, view, categoryViews = CP.StateBoundary.CATEGORY_VIEWS) => {
  for (const entry of categoryViews) {
    state.expanded[CP.StateBoundary.categoryKey(project, entry)] = (entry === view);
  }
};

CP.StateBoundary.normalizeProjectCategoryExpanded = (state, project, categoryViews = CP.StateBoundary.CATEGORY_VIEWS) => {
  if (!project) return;
  const opened = categoryViews.filter((view) => !!state.expanded[CP.StateBoundary.categoryKey(project, view)]);
  if (opened.length <= 1) return;
  CP.StateBoundary.openProjectCategory(state, project, opened[0], categoryViews);
};

CP.StateBoundary.viewToCategory = (view, categoryViews = CP.StateBoundary.CATEGORY_VIEWS) => {
  if (!view) return null;
  if (categoryViews.includes(view)) return view;
  if (view === 'session') return 'sessions';
  if (view === 'task') return 'tasks';
  if (view === 'job') return 'jobs';
  return null;
};

CP.StateBoundary.pivotProjectAliases = (state, project) => {
  state.tasks = (project && state.tasksByProject[project]) || [];
  state.jobs = (project && state.jobsByProject[project]) || [];
};

CP.StateBoundary.mergeTaskIntoState = (state, task) => {
  if (!task || !task.id) return;
  const patch = (arr) => {
    if (!Array.isArray(arr)) return;
    const idx = arr.findIndex((item) => item.id === task.id);
    if (idx >= 0) arr.splice(idx, 1, { ...arr[idx], ...task });
  };
  patch(state.tasks);
  if (task.project) patch(state.tasksByProject[task.project]);
};
