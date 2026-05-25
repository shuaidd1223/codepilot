"""Structured payload types shared between the Web UI backend and frontend.

Prior to this module every webui endpoint returned a free-form ``dict``,
which meant the frontend (`codepilot/web/app.js` and the Vue components)
had to discover field names by reading Python source. That made both
sides brittle: adding a new column to ``tasks`` and forgetting to update
one place silently broke the UI.

These ``TypedDict`` declarations are *documentation with teeth*:

* They run through ``mypy`` / IDE type-checkers when available.
* The test suite asserts every key declared here is actually emitted
  by :mod:`codepilot.webui_payloads` (and vice versa).
* :func:`to_json_schema` exposes a JSON Schema view so a future build
  step could emit TypeScript types for the frontend.

This is intentionally a *description* of current behaviour; adding or
removing fields is a breaking change that should be done with the
tests in ``tests/test_webui_schema.py`` updated in the same commit.
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


# ── Atomic building blocks ────────────────────────────────────────────────


class TaskActions(TypedDict):
    """Which buttons the UI should enable for this task."""

    retry: bool
    stop: bool
    promote: bool
    cancel: bool
    archive: bool
    delete: bool


class WorkItemPayload(TypedDict):
    """Normalized intake metadata attached to tasks."""

    source: str
    requester: str
    context_links: list[Any]
    callback: dict[str, Any]
    raw_text: str
    raw_text_summary: str


class ReviewerAcCheck(TypedDict):
    """One AC row from a parsed reviewer verdict."""

    id: str
    status: str  # "PASS" | "FAIL" | "N/A"
    reason: str


class ReviewerVerdictBlock(TypedDict):
    """Structured reviewer decision extracted from a reviewer phase log.

    Populated by ``codepilot.commands.reviewer_output.parse_reviewer_output``
    and attached to each reviewer-phase :class:`TaskLogEntry` plus a
    top-level ``latest_review`` field on :class:`TaskDetail`, so the
    dashboard can render a first-class verdict panel instead of the raw
    transcript dump.
    """

    verdict: str  # "pass" | "fail" | "unknown"
    source: str  # "json" | "legacy" | "empty"
    ac_checks: list[ReviewerAcCheck]
    blockers: list[str]
    advisory: list[str]


class ReviewerVerdictSummary(ReviewerVerdictBlock, total=False):
    """Verdict block augmented with the phase/agent that produced it."""

    phase: str
    agent: str


class TaskLogEntry(TypedDict):
    """One row from ``task_logs`` rendered for the UI."""

    phase: str
    agent: str
    exit_code: Optional[int]
    output_excerpt: str
    review: Optional[ReviewerVerdictBlock]


class TaskPhaseLogEntry(TypedDict):
    """One task-detail process phase with lazy raw-log metadata."""

    key: str
    index: int
    kind: str
    phase: str
    label: str
    round: Optional[int]
    agent: str
    status: str
    active: bool
    exit_code: Optional[int]
    started_at: str
    finished_at: str
    duration: Optional[int]
    summary: str
    output_excerpt: str
    raw_available: bool
    raw_source: str
    raw_size: int
    raw_filename: str
    default_expanded: bool
    review: Optional[ReviewerVerdictBlock]


# ── List-view task payload (dashboard) ────────────────────────────────────


class TaskListItem(TypedDict):
    """Shape emitted by :func:`codepilot.webui_payloads._task_payload`.

    Used in the dashboard task grid. Detail view augments it via
    ``TaskDetail`` below — the two share this common base.
    """

    id: int
    project: str
    title: str
    status: str
    priority: str
    agent: str
    source: str
    work_item: WorkItemPayload
    phase: str
    runtime: str
    eta_seconds: Optional[int]
    latest: str
    error_message: str
    skip_reason: str
    blocked_reason: Optional[str]
    suggested_actions: Optional[list[str]]
    delivery_record: str
    created_at: str
    started_at: str
    completed_at: str
    updated_at: str
    execution_status: str
    retry_count: int
    max_retries: int
    recovery_hints: list[str]
    actions: TaskActions
    workflow_column_id: str
    workflow_column_title: str


# ── Task detail payload ───────────────────────────────────────────────────


class TaskDetail(TaskListItem):
    """Detailed task payload returned by ``/api/task/:id``."""

    content: str
    depends_on: list[int]
    project_path: str
    current_log_path: str
    log_text: str
    logs: list[TaskLogEntry]
    phase_logs: list[TaskPhaseLogEntry]
    latest_review: Optional[ReviewerVerdictSummary]
    artifacts: dict[str, Any]
    execution_artifact: Optional[dict[str, Any]]
    timeline: list[dict[str, Any]]


# ── Project-level payloads ────────────────────────────────────────────────


class ProjectStats(TypedDict):
    """Aggregate task counts per project."""

    backlog: int
    in_progress: int
    done: int
    failed: int
    cancelled: int
    total: int


class ProjectServiceStatus(TypedDict):
    """Per-project background service state shown in the project view."""

    running: bool
    pid: int
    project: str
    started_at: str
    log: str


class ProjectDaemonServiceStatus(ProjectServiceStatus, total=False):
    """Task polling daemon state.

    ``stopping`` is emitted by newer daemon builds while a graceful stop has
    been requested and the current task is allowed to finish.
    """

    stopping: bool


class ProjectServices(TypedDict):
    """Background services controlled per project."""

    tasks: ProjectDaemonServiceStatus
    inspect: ProjectServiceStatus


class ProjectWorkflowAutoPolicy(TypedDict):
    """Resolved workflow auto-advance policy shown in the project view."""

    allow_create_inspect_tasks: bool
    allow_import_plan_tasks: bool
    max_steps: int
    failure_threshold: int


class ProjectWorkflowInspect(TypedDict):
    """Latest inspect workflow context exposed to the project view."""

    context_path: str
    quality_summary: dict[str, Any]
    created_preview: list[dict[str, Any]]
    report_only: list[dict[str, Any]]
    ignored_report_only: list[dict[str, Any]]
    deleted_report_only: list[dict[str, Any]]
    archived_report_only: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    source_command: str


class ProjectWorkflow(TypedDict):
    """Latest workflow state and available actions for a project."""

    status: dict[str, Any]
    next_actions: list[dict[str, Any]]
    auto_policy: ProjectWorkflowAutoPolicy
    inspect: ProjectWorkflowInspect


class ProjectSummary(TypedDict):
    """One row in the dashboard's project selector."""

    name: str
    path: str
    stats: ProjectStats
    session_count: int
    job_count: int
    active_summary: str
    services: ProjectServices
    workflow: ProjectWorkflow


class DashboardPayload(TypedDict):
    """Top-level envelope returned by ``/api/dashboard``.

    ``tasks_by_project`` / ``jobs_by_project`` ship every project's data in
    one response so the frontend can hydrate its per-project cache once;
    project switching then becomes a purely navigational operation.
    ``tasks`` / ``jobs`` remain as convenience aliases for the currently-
    selected project's slice.
    """

    projects: list[ProjectSummary]
    selected_project: Optional[str]
    tasks_by_project: dict[str, list[TaskListItem]]
    task_board_by_project: dict[str, dict[str, Any]]
    task_board: dict[str, Any]
    jobs_by_project: dict[str, list[dict]]
    tasks: list[TaskListItem]
    jobs: list[dict]    # Job schema still lives in webui; tracked separately.
    events: list[dict]  # Event schema intentionally loose for now.


# ── Schema introspection ──────────────────────────────────────────────────


def _annotation_to_json_schema(annotation: Any) -> dict:
    """Best-effort translation of a TypedDict annotation into JSON Schema.

    Good enough for tooling / docs generation; not a full type system.
    Unknown types fall through to ``{"type": "object"}``.
    """
    # Strip Optional[X] to X + nullable marker.
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        args = getattr(annotation, "__args__", ())
        # Optional[X] is Union[X, None].
        if origin.__name__.lower() == "union" or str(origin) in {"typing.Union"}:
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                inner = _annotation_to_json_schema(non_none[0])
                return {**inner, "nullable": True}
        if origin in (list, tuple):
            inner = _annotation_to_json_schema(args[0]) if args else {"type": "object"}
            return {"type": "array", "items": inner}
        if origin is dict:
            return {"type": "object"}

    # Plain TypedDict subclasses carry __annotations__.
    if hasattr(annotation, "__annotations__") and hasattr(annotation, "__total__"):
        return to_json_schema(annotation)

    mapping = {
        int: {"type": "integer"},
        float: {"type": "number"},
        str: {"type": "string"},
        bool: {"type": "boolean"},
    }
    return mapping.get(annotation, {"type": "object"})


def to_json_schema(td: type) -> dict:
    """Return a JSON Schema view of a TypedDict.

    Useful for generating frontend types or validating that API
    responses haven't drifted. Only captures shape, not enum values.
    """
    props: dict[str, dict] = {}
    for key, annotation in td.__annotations__.items():
        props[key] = _annotation_to_json_schema(annotation)
    return {
        "type": "object",
        "title": td.__name__,
        "properties": props,
        "required": list(props.keys()),
    }


def payload_keys(td: type) -> set[str]:
    """Return the complete set of keys a TypedDict declares (incl. inherited)."""
    keys: set[str] = set()
    for base in reversed(td.__mro__):
        keys.update(getattr(base, "__annotations__", {}).keys())
    return keys
