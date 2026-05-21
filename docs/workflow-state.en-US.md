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

## Agent Kernel Session State

Since v0.7.x, CodePilot introduces an Agent Kernel session state layer that tracks the full agent workflow lifecycle. A session spans nine phases: `intake → clarify → explore → plan → approve → execute → review → recover → deliver`.

### Session Fields

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `session_id` | string | Unique session identifier (`sess_<hex>`). |
| `goal` | string | Original user goal description. |
| `current_phase` | string | Current phase name, initially `intake`. |
| `phase_history` | array | Phase history entries with `phase`, `entered_at`, `exited_at`. |
| `blocked_reason` | string/null | Blocked reason; `null` when not blocked. |
| `next_actions` | array | Suggested next action strings. |
| `linked_task_ids` | array | Associated task IDs. |
| `artifact_paths` | object | Readable artifact path collection. |
| `started_at` | string | ISO timestamp of session creation. |
| `updated_at` | string | ISO timestamp of last update. |
| `completed_at` | string/null | Completion timestamp; `null` when active. |

### Internal API

New API in `codepilot/core/workflow_state.py`:

- `create_agent_session(project_path, *, goal, session_id=None)`: Create a session starting at `intake` phase.
- `get_agent_session(project_path)`: Read the active session; missing or corrupt JSON returns `None`.
- `update_agent_session(project_path, **changes)`: Update session fields, auto-refresh `updated_at`.
- `advance_agent_phase(project_path, phase, **extra_fields)`: Advance to next phase, closing the previous entry.
- `fail_agent_session(project_path, *, blocked_reason, next_actions=None)`: Mark session as blocked.
- `complete_agent_session(project_path)`: Mark session as completed and close the final phase.
- `cleanup_agent_session(project_path)`: Remove the session state file.

### Storage

Session state is stored at `.codepilot/state/agent-session.json`.

### CLI JSON Contract

The `workflow status --json` output includes an `agent_session` field alongside the mode-level `state` field:

```json
{
  "ok": true,
  "command": "workflow status",
  "data": {
    "project": "demo",
    "project_path": "D:\\myCode\\demo",
    "mode": null,
    "state": { "mode": "clarify", "active": true },
    "agent_session": {
      "session_id": "sess_abc123def456",
      "goal": "Add user login",
      "current_phase": "clarify",
      "phase_history": [
        {"phase": "intake", "entered_at": "...", "exited_at": "..."},
        {"phase": "clarify", "entered_at": "...", "exited_at": null}
      ],
      "blocked_reason": null,
      "next_actions": [],
      "linked_task_ids": [],
      "artifact_paths": {
        "context": ".codepilot/context/sess_abc123def456.json",
        "spec": ".codepilot/specs/sess_abc123def456.md",
        "plan": ".codepilot/plans/sess_abc123def456.md"
      },
      "started_at": "...",
      "updated_at": "...",
      "completed_at": null
    }
  }
}
```

## Relationship To The Existing Task DB

The workflow state layer does not replace the SQLite task queue. It is a lightweight side channel for pre-task artifacts and context. Once work becomes executable backlog, task status, logs, retries, dependencies, and delivery records still live in the task database and task log storage.
