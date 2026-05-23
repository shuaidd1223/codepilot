---
name: codepilot-workflow
description: "Use this skill when an AI agent needs to run CodePilot as a local engineering workflow orchestrator: answer project/task/service questions from local data, create plans, convert explicit requirements into tasks, inspect status and logs, operate daemon/inspect/ui/feishu/webhook/event/hook/provider services, manage local skill catalog entries, and prepare binary releases. Trigger for requirement intake, project status questions, task retry/stop/show/logs, queue execution, read-only exploration, wiki/note/trace use, Feishu or webhook integration, AI manifest/template usage, and release packaging/verification."
author: "帅呆呆 <2264505396@qq.com>"
repository: "https://gitee.com/shuai_dd/workflow"
license: "MIT"
---

# CodePilot Workflow

**Author:** 帅呆呆 <2264505396@qq.com> | **Repository:** https://gitee.com/shuai_dd/workflow | **License:** MIT

## Quick Start

Use non-interactive commands by default.

1. Classify the request as a question, plan, explicit work creation, task operation, service operation, or release flow.
2. Prefer JSON output for machine reasoning.
3. Use read-only commands before planning when evidence is needed.
4. Operate tasks only through `codepilot task ...`.
5. Use `codepilot binary ...` for build and release flows.

## Standard Flow

### 1) Prepare Or Check The Project

```bash
codepilot setup . --dry-run --json
codepilot setup .
codepilot doctor --project <project> --services --json
```

### 2) Answer Questions First

Treat status, totals, completion, failed tasks, running tasks, project lists, and service health as questions.

```bash
codepilot go "What is the current project status?" -p <project>
codepilot status -p <project> --json
codepilot hud -p <project> --preset full --json
codepilot trace -p <project> --limit 30 --json
```

If evidence is needed before answering or planning:

```bash
codepilot explore -p <project> --prompt "question or search terms" --json
codepilot wiki query -p <project> "keyword" --json
codepilot note show -p <project> --json
```

`explore` is read-only. It must not be used to modify files, run tests, start services, install dependencies, or mutate Git state.

### 3) Plan Before Creating Work

```bash
codepilot plan -p <project> "clear requirement" --json
```

`plan` produces artifacts only. It does not create backlog items or start executors.

### 4) Create Explicit Work

For one-shot requirement intake:

```bash
codepilot "<requirement>"
codepilot go "<requirement>" -p <project>
```

In `chat`, Web UI sessions, and Feishu free text, CodePilot routes free text into OpenCode + CodePilot MCP. Use plain natural language and let the OpenCode session call CodePilot tools, or call `plan` / structured MCP tools explicitly when an artifact is required.

```text
What is the current project status?
Improve the Feishu webhook card content.
Before fixing failed task 12, list the candidates and risks.
```

Interactive OpenCode sessions are stateful and use CodePilot-managed isolated runtime files under `~/.codepilot/opencode/<project-scope>/`, including generated config, MCP wiring, session data, and project-level model selection.

### 5) Observe And Intervene

```bash
codepilot task show <task_id> --json
codepilot task logs <task_id> --tail 80
codepilot task find <keyword> -p <project> --json
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task edit <task_id> --status backlog
codepilot task rm <task_id>
```

For a failed-task repair loop:

```bash
codepilot build-fix -p <project> --task-id <task_id> --dry-run
codepilot build-fix -p <project> --task-id <task_id> --json
```

### 6) Execute Or Operate Services

```bash
codepilot run -p <project> --once
codepilot daemon -p <project>
codepilot daemon -p <project> --status
codepilot daemon -p <project> --stop
codepilot inspect -p <project> --once --json
codepilot inspect -p <project> --status
codepilot ui start
codepilot ui status
codepilot ui logs --tail 100
```

### 7) Integrations

```bash
codepilot feishu start
codepilot feishu status
codepilot feishu logs --tail 100
codepilot webhook --host 127.0.0.1 --port 8765
codepilot event schema --json
codepilot event list -p <project> --json
codepilot hook validate -p <project> --json
codepilot exec -p <project> --provider codex --dry-run --json -- codex --version
codepilot skill list -p <project> --json
```

## Direct Task Injection

Only use `add` when an external agent has already planned concrete tasks.

```bash
codepilot ai template --format json
codepilot add -p <project> -t "task title"
codepilot add -p <project> -f tasks.json
codepilot add -p <project> -f tasks.md
```

Hard rules:

- `tasks.json` entries must include task-template compliant `content`.
- `tasks.md` sections separated by `---` must each be complete task-template content.
- `add -t` and `tasks.txt` invoke the configured AI to generate compliant content.
- Empty placeholders such as `--no-ai` and `--allow-empty` are removed.

## Release Flow

Use only the `binary` command group:

```bash
codepilot binary build
codepilot binary install --binary <binary-path>
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <version>
codepilot binary where
```

## Hard Constraints

- Do not use removed command forms:
  - `codepilot release ...`
  - top-level `codepilot show/logs/stop/retry/find/...`
  - `codepilot webui ...`
- Always use:
  - `codepilot task <subcommand>`
  - `codepilot binary <subcommand>`
  - `codepilot ui <subcommand>`
- Prefer `--json` outputs for machine-agent integration.
- Treat project status, task totals, failed/running tasks, service status, and command usage questions as questions first.
- In interactive channels, use plain OpenCode conversation or explicit CLI/MCP calls.

## References

- Command map and minimum command set: [references/command-map.md](references/command-map.md)
- Integration playbooks and error handling: [references/agent-playbooks.md](references/agent-playbooks.md)
