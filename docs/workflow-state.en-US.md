# CodePilot Workflow Mode State And Artifact Directory Conventions

Language: [中文](workflow-state.zh-CN.md) | English

This document defines the project-local state layer shared by first-stage workflow commands. It supports `clarify`, `plan`, `explore`, `wiki`, and related modes so they can reuse context, recover state, and emit machine-readable artifacts.

## Design Goals

- Keep workflow state independent from the existing task queue database.
- Store per-project workflow files under the project root `.codepilot/` directory.
- Make state readable by humans and automation.
- Keep artifacts durable enough for later review, but avoid treating them as task queue records.

## Directory Semantics

| Path | Purpose |
| :--- | :--- |
| `.codepilot/state/` | Active workflow state files and active-mode pointers. |
| `.codepilot/context/` | Reusable context bundles collected by read-only workflow commands. |
| `.codepilot/specs/` | Requirement specification artifacts produced by `clarify` and related commands. |
| `.codepilot/plans/` | Reviewable execution plan artifacts produced by `plan`. |

The directory layout belongs to project-local workflow state. It does not replace global CodePilot data, task queue storage, logs, or service state.

## State Fields

Workflow state is JSON. Common fields include:

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `mode` | string | Workflow mode such as `clarify`, `plan`, or `explore`. |
| `active` | boolean | Whether this mode currently has active state. |
| `status` | string | Current workflow status, for example `active`, `completed`, or `interrupted`. |
| `started_at` | string | ISO timestamp when the workflow state was created. |
| `updated_at` | string | ISO timestamp of the latest update. |
| `artifact_path` | string | Optional path to the spec/plan/context artifact created by the workflow. |
| `metadata` | object | Mode-specific structured data. |

## Internal API

The workflow state helper layer provides:

- `workflow_dirs(project_path)`: return the directory convention for a project.
- `ensure_workflow_dirs(project_path)`: create `.codepilot/state|context|specs|plans`.
- `start_workflow(project_path, mode, **metadata)`: create or refresh active state.
- `read_workflow_state(project_path, mode=None)`: read active or mode-specific state; missing or invalid JSON returns `None`.
- `update_workflow_state(project_path, mode, **changes)`: update a mode state while preserving `started_at` and refreshing `updated_at`.
- `complete_workflow(project_path, mode)`: mark a mode as inactive/completed and clear the active pointer.
- `cleanup_workflow_states(project_path, completed=True)`: remove inactive completed state files.

## CLI View

Workflow commands may expose state through their own `--json` output. A typical state payload looks like:

```json
{
  "project_path": "D:\\myCode\\demo",
  "mode": "plan",
  "active": true,
  "status": "active",
  "artifact_path": ".codepilot/plans/20260520-example.md"
}
```

Consumers should treat state files as best-effort workflow metadata, not as the source of truth for queued tasks.

## Relationship To The Existing Task DB

The workflow state layer does not replace the SQLite task queue. It is a lightweight side channel for pre-task artifacts and context. Once work becomes executable backlog, task status, logs, retries, dependencies, and delivery records still live in the task database and task log storage.
