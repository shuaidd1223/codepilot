---
name: codepilot-workflow
description: "Use this skill when an AI agent needs to run CodePilot as a local engineering workflow orchestrator: convert natural-language requirements into tasks, inspect task status and logs, operate project-level execution services, and prepare binary releases. Trigger for requests like requirement intake, task retry/stop/show/logs, queue execution, daemon/inspect/ui operations, and release packaging/verification."
---

# CodePilot Workflow

## Quick Start

Use non-interactive commands by default.

1. Initialize or verify project registration.
2. Submit requirement via natural-language entrypoint.
3. Read status and task detail in JSON mode.
4. Operate tasks only through `codepilot task ...`.
5. Use `codepilot binary ...` for build/release flows.

## Standard Execution Flow

### 1) Intake

Run one of:

```bash
codepilot "<requirement>"
codepilot go "<requirement>"
```

### 2) Observe

```bash
codepilot status -p <project> --json
codepilot task show <task_id> --json
codepilot task logs <task_id> --tail 80
```

### 3) Intervene

```bash
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task edit <task_id> --status backlog
```

### 4) Execute Backlog Explicitly

```bash
codepilot run -p <project>
```

### 5) Project Services (optional)

```bash
codepilot daemon -p <project>
codepilot daemon -p <project> --status
codepilot daemon -p <project> --stop
codepilot inspect -p <project> --status
codepilot ui status
```

## Release Flow

Use only `binary` command group:

```bash
codepilot binary build
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <version>
```

## Hard Constraints

- Do not use removed command forms:
  - `codepilot release ...`
  - top-level `codepilot show/logs/stop/retry/find/...`
- Always use:
  - `codepilot task <subcommand>`
  - `codepilot binary <subcommand>`
- Prefer `--json` outputs for machine-agent integration.

## References

- Command map and minimum command set: [references/command-map.zh-CN.md](references/command-map.zh-CN.md)
- Integration playbooks and error handling: [references/agent-playbooks.zh-CN.md](references/agent-playbooks.zh-CN.md)
