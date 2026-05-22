# Multi-Project Task Execution And Inspection Service Notes

Language: [中文](project-services.md) | English

This document records the current state, operating model, boundaries, and follow-up work for CodePilot's multi-project task execution and inspection services.

## Background Goal

The old execution model was close to a single global daemon: start one background process and let it poll task queues. That was hard to reason about in multi-project setups and made it difficult to control execution and inspection per project. The new model makes task execution and inspection project-scoped services and exposes them in the Web UI.

## Completed Work

### 1. Task Execution Daemon Is Project-Scoped

Each project can run its own task polling service. The daemon stores project-local runtime state and can be started, queried, and stopped by project.

Typical commands:

```bash
codepilot daemon -p <project-name>
codepilot daemon -p <project-name> --status
codepilot daemon -p <project-name> --stop
```

### 2. Task Polling Stop Uses Graceful Shutdown

Stopping a daemon asks the project service to exit cleanly when possible, reducing the chance of partially written state.

### 3. Inspection Service Is Project-Scoped

Inspection is controlled per project instead of being tied to a single global process.

```bash
codepilot inspect -p <project-name> --once
codepilot inspect -p <project-name> --once --dry-run --write-workflow --json
codepilot inspect -p <project-name> --status
codepilot inspect -p <project-name> --stop
```

`--write-workflow` only works with `--once --dry-run`. It writes the inspection preview to project-local `.codepilot/context/`, syncs the Agent Session, and exposes safe actions through `workflow next`; it does not write backlog tasks.

### 4. Web UI Project Page Includes Service Controls

The Web UI can show per-project execution/inspection state and provide start/stop controls. It is intended to make multi-project operation visible without switching terminal sessions.

### 5. Web UI Startup Behavior Was Adjusted

The Web UI can run independently from project daemons. Starting the UI does not imply that every project service is running.

## Current Usage

Start task polling for one project:

```bash
codepilot daemon -p <project-name>
```

Check task polling status:

```bash
codepilot daemon -p <project-name> --status
```

Stop task polling:

```bash
codepilot daemon -p <project-name> --stop
```

Run one inspection:

```bash
codepilot inspect -p <project-name> --once
codepilot inspect -p <project-name> --once --dry-run --write-workflow --json
```

Check inspection state:

```bash
codepilot inspect -p <project-name> --status
```

Start the Web UI:

```bash
codepilot ui
codepilot ui start
```

## Verified Behavior

- Multiple projects can have independent execution and inspection state.
- Project service status can be read from CLI and Web UI.
- Task execution and inspection control no longer rely on a single global daemon.
- Web UI can be started without forcing project task polling to start.

## Current Boundaries And Known Issues

### 1. Inspection Stop Can Still Be Forceful

Inspection shutdown is not as graceful as task polling in every path. When a process does not respond, CodePilot may still terminate it forcefully.

### 2. Legacy Periodic Inspection Entrypoints Remain In The Daemon

Some older periodic inspection hooks still exist for compatibility. New project-scoped inspection should be preferred.

### 3. Web UI Does Not Expose Every Inspection Option

The CLI remains the full-control surface for fine-grained inspection configuration.

### 4. Web UI Service Logs Are Not Fully Surfaced

Service logs are still primarily accessed through CLI log commands and filesystem log files.

### 5. Service Status Depends On Local Files And PID Checks

Status is pragmatic and local-machine oriented. It is not a distributed service registry.

### 6. Global Resource Limits Are Still Basic

Multi-project parallelism needs better global concurrency and cost controls.

### 7. Web UI Health Banner Is Still A Global Summary

The health banner is useful for quick checks, but detailed diagnosis still requires per-project service views.

## Follow-Up Checklist

### P0

- Keep daemon and inspection stop behavior reliable.
- Make service status reporting explicit when state files are stale.

### P1

- Expose service logs in the Web UI.
- Add clearer project service health states.

### P2

- Improve global concurrency and resource guardrails.
- Expand Web UI controls for inspection configuration.

### P3

- Continue reducing legacy global-service assumptions.
- Improve documentation and operational examples as behavior changes.

## Suggested Implementation Order

1. Stabilize stop/status semantics.
2. Add log visibility.
3. Improve Web UI service controls.
4. Add global resource guardrails.
5. Retire compatibility-only service paths when callers are migrated.
