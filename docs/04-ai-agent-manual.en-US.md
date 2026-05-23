# CodePilot AI And Agent Calling Guide

Language: [中文](04-AI与Agent调用手册.zh-CN.md) | English

This guide is for other AI agents and automation systems. Its goal is to make project status reads, task creation, troubleshooting, and service integration stable and repeatable.

## 1. Calling Principles

1. Prefer non-interactive commands unless a persistent session is explicitly needed.
2. Prefer `--json` when structured output is needed, or read `codepilot ai manifest`.
3. Submit high-level requirements with `codepilot "requirement text"` or `codepilot go "requirement text"`.
4. Treat project status, task counts, completion ratio, failed tasks, running tasks, and service status as Q&A.
5. `chat`, Web UI sessions, and Feishu free text route through OpenCode + CodePilot MCP.
6. Use `codepilot task ...` for task operations.
7. Use `codepilot binary ...` for release operations.
8. Before directly importing tasks, read `codepilot ai template --format json`.
9. Use `plan` explicitly when a deterministic plan artifact is required.

## 2. Minimal Command Set

### 2.1 Project Preparation

```bash
codepilot setup . --dry-run --json
codepilot setup .
codepilot doctor --project <project-name> --services --json
```

### 2.2 Requirements and Planning

```bash
codepilot "fix task retry logic and add tests"
codepilot go "fix task retry logic and add tests" -p <project-name>
codepilot plan -p <project-name> "clear requirement" --json
codepilot plan -p <project-name> --from-spec .codepilot/specs/example.md --json
codepilot workflow status -p <project-name> --json
codepilot workflow next -p <project-name> --list --json
codepilot workflow next -p <project-name> --action <id> --json
codepilot workflow next -p <project-name> --auto --json
```

`plan` does not create backlog tasks and does not start execution.

Their JSON output records `next_actions` for follow-up work such as generating a plan or importing tasks. External AI agents should inspect actions with `workflow next --list`, advance by id with `workflow next --action <id>`, or let CodePilot choose policy-allowed auto actions with `workflow next --auto`. `suggested_command` is only display/review metadata; do not compose or execute it automatically.

The default `workflow next --auto` policy only allows conservative low-risk actions: ignoring inspection report items that already received negative feedback, and turning an inspect context into a reviewable plan. It does not create inspect tasks or import plan tasks unless the project opts in through `[automation]`:

```toml
workflow_auto_create_inspect_tasks = false
workflow_auto_import_plan_tasks = false
workflow_auto_max_steps = 1
workflow_auto_failure_threshold = 1
```

### 2.3 Status, Evidence, Memory

```bash
codepilot status -p <project-name> --json
codepilot hud -p <project-name> --preset full --json
codepilot explore -p <project-name> --prompt "find task template" --json
codepilot trace -p <project-name> --limit 30 --json
codepilot wiki query -p <project-name> "build" --json
codepilot note show -p <project-name> --json
codepilot memory events -p <project-name> --json
```

`explore` is read-only and does not write files, change Git, start services, install dependencies, or run tests.

`memory events` reads the project-local automatic observation log. CodePilot automatically turns facts into deduplicated candidates and maintains `.codepilot/memory/autocapture.md`; candidates record `score`, `feedback`, and `seen_count`, with workflow actions and terminal task outcomes automatically adjusting weight. It does not directly write human-maintained long-term wiki/note content.

### 2.4 Task Control

```bash
codepilot task show <task_id> --json
codepilot task logs <task_id> --tail 80
codepilot task find <keyword> -p <project-name> --json
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task rm <task_id>
```

### 2.5 Execution, Background Services, Repair Loop

```bash
codepilot run -p <project-name> --once
codepilot daemon -p <project-name>
codepilot daemon -p <project-name> --status
codepilot inspect -p <project-name> --once
codepilot inspect -p <project-name> --once --dry-run --write-workflow --json
codepilot inspect -p <project-name> --status
codepilot build-fix -p <project-name> --task-id <task_id> --json
```

`inspect --write-workflow` only works with `--once --dry-run`. It writes the inspection result to project-local workflow context and exposes safe `next_actions` such as `create_inspect_tasks`, `promote_inspect_report_<candidate_id>`, `ignore_inspect_report_<candidate_id>`, `delete_inspect_report_<candidate_id>`, `archive_inspect_report_<candidate_id>`, and `plan_from_inspect`; it does not create backlog items or start the executor.

### 2.6 Web UI, Feishu, Webhook

```bash
codepilot ui
codepilot ui start
codepilot ui logs --tail 100
codepilot feishu start
codepilot feishu status
codepilot webhook --host 127.0.0.1 --port 8765
```

### 2.7 Events, Hooks, Providers, Skills

```bash
codepilot event schema --json
codepilot hook validate -p <project-name> --json
codepilot exec -p <project-name> --provider codex --dry-run --json -- codex --version
codepilot skill list -p <project-name> --json
```

### 2.8 Release And Installation

```bash
codepilot binary prepare --version <version>
codepilot binary verify
codepilot binary where
```

### 2.9 Machine-Readable Documentation

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
codepilot ai template --format json
codepilot ai template --format guide
```

### 2.10 MCP Default Tool Contract

`codepilot mcp serve --list-tools` and MCP `list_tools` use the same default registry. The default public tool count is 25, excluding the runtime health-check tool `codepilot.health`. When a real MCP service starts, `codepilot.health` is registered in addition to these default tools for liveness checks.

Default public tools:

- `archive_task`
- `build_fix`
- `create_task`
- `daemon_status`
- `doctor`
- `edit_task`
- `exec`
- `explore`
- `feishu_notify`
- `feishu_send_to_user`
- `generate_breakdown`
- `hook_trigger`
- `inspect_project`
- `inspect_workflow`
- `list_tasks`
- `note_add`
- `run_once`
- `show_task`
- `stop_task`
- `validate_task_template`
- `webhook_invoke`
- `wiki_add`
- `wiki_query`
- `workflow_next`
- `workflow_status`

## 3. Direct Task Submission

Direct task import is for external AI systems that have already planned work and rendered complete task content. It is not the normal human workflow.

```bash
codepilot ai template --format json
codepilot add -p <project-name> -f tasks.json
codepilot add -p <project-name> -f tasks.md
```

Rules:

- Every imported task needs a complete `content` body.
- The content must satisfy the task template section contract.
- Empty placeholders and unreplaced `{goal}`-style tokens are rejected.
- If the task is still just a title, use the natural-language requirement entrypoint instead.

## 4. Recommended Workflows

### 4.1 Submit A Requirement And Track It

```bash
codepilot "fix retry handling and add regression tests"
codepilot status -p <project-name> -v
codepilot task show <task_id>
codepilot task logs <task_id> --tail 80
```

### 4.2 Plan Before Executing

```bash
codepilot workflow next -p <project-name> --list --json
codepilot workflow next -p <project-name> --action plan_from_spec --json
codepilot workflow next -p <project-name> --action import_tasks --json
codepilot workflow next -p <project-name> --auto --json
```

Review the listed `next_actions` before executing one, or use `workflow next --auto` to let CodePilot choose policy-allowed auto actions. `workflow next` uses a fixed allowlist and does not shell out to `suggested_command`; high-risk actions require explicit confirmation and must still be allowlisted.

### 4.3 Answer Project Questions

```bash
codepilot go "how many tasks are failed?" -p <project-name>
codepilot explore -p <project-name> --prompt "recent failed task logs" --json
```

### 4.4 Recover A Failed Task

```bash
codepilot task show <task_id> --json
codepilot task logs <task_id> --tail 80
codepilot build-fix -p <project-name> --task-id <task_id> --json
```

### 4.5 Pre-Release Check

```bash
codepilot doctor --project <project-name> --services --json
codepilot binary prepare --version <version>
codepilot binary verify
```

## 5. JSON Output Contract

Prefer JSON for automation. Stable commands include:

- `codepilot ai manifest`
- `codepilot status -p <project-name> --json`
- `codepilot hud -p <project-name> --json`
- `codepilot workflow status -p <project-name> --json`
- `codepilot workflow next -p <project-name> --list --json`
- `codepilot workflow next -p <project-name> --action <id> --json`
- `codepilot workflow next -p <project-name> --auto --json`
- `codepilot task show <task_id> --json`
- `codepilot task find <keyword> -p <project-name> --json`
- `codepilot doctor --json`
- `codepilot event schema --json`
- `codepilot hook validate -p <project-name> --json`
- `codepilot ai template --format json`

## 6. Compatibility Notes

- Old top-level task commands have moved under `codepilot task ...`.
- Release commands have moved under `codepilot binary ...`.
- Legacy `codepilot webui ...` has moved to `codepilot ui ...`.
- `add --no-ai` and `add --allow-empty` are removed.
- OpenCode runtime files are generated under `~/.codepilot/opencode/<project-id>/`.

## 7. Using The Skill

The reusable Skill lives at `skills/codepilot-workflow/SKILL.md`. It is intentionally written in English so other AI runtimes can reuse it. Installation and maintenance notes are documented in the Skill guide: [中文](05-Skill化集成指南.zh-CN.md) / [English](05-skill-integration-guide.en-US.md).
