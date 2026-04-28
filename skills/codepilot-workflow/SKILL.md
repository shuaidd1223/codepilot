---
name: codepilot-workflow
description: "Use this skill when an AI agent needs to run CodePilot as a local engineering workflow orchestrator: answer project/task status questions from local workflow data, convert explicit natural-language requirements into tasks, inspect task status and logs, operate project-level daemon/inspect/ui/feishu/webhook services, and prepare binary releases. Trigger for requests like requirement intake, task retry/stop/show/logs, queue execution, daemon/inspect/ui operations, Feishu bot control, webhook integration, AI manifest/template usage, and release packaging/verification."
---

# CodePilot Workflow

## Quick Start

Use non-interactive commands by default.

1. Initialize or verify project registration.
2. Distinguish question vs work creation before acting.
3. Read status and task detail in JSON mode.
4. Operate tasks only through `codepilot task ...`.
5. Use `codepilot binary ...` for build/release flows.

## Standard Execution Flow

### 1) Intake Or Ask

For one-shot CLI requirement intake, run:

```bash
codepilot "<requirement>"
codepilot go "<requirement>"
```

For project/status questions, ask through the natural-language entrypoint or inspect JSON directly:

```bash
codepilot go "How many tasks exist and how many are done?" -p <project>
codepilot status -p <project> --json
```

In `chat`, Web UI sessions, and Feishu free text, do not rely on ambiguous text to create work. Use explicit prefixes:

```text
? What is the current project status?
# Fix the Feishu webhook card content
! Fix one specific small issue
```

If free text looks like a requirement but lacks an explicit prefix, CodePilot returns a confirmation prompt and does not execute.

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
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
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
codepilot feishu status
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
- Treat project status, task totals, failed/running tasks, service status, and command usage questions as questions first.
- In interactive channels, only create work when the user explicitly uses `#` for a requirement or `!` for a small task.

## References

- Command map and minimum command set: [references/command-map.md](references/command-map.md)
- Integration playbooks and error handling: [references/agent-playbooks.md](references/agent-playbooks.md)
