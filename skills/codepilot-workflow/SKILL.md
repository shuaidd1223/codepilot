---
name: codepilot-workflow
description: "Use this skill when an AI agent needs to run CodePilot as a local engineering workflow orchestrator: answer project/task/service questions from local data, create plans, convert explicit requirements into tasks, inspect status and logs, operate daemon/inspect/ui/feishu/webhook/event/hook/provider services, manage local skill catalog entries, use MCP tools directly, manage memory/autocapture, and prepare binary releases. Trigger for requirement intake, project status questions, task retry/stop/show/logs, queue execution, read-only exploration, wiki/note/trace/memory use, Feishu or webhook integration, MCP tool operations, AI manifest/template usage, self-update audits, and release packaging/verification."
author: "帅呆呆 <2264505396@qq.com>"
repository: "https://gitee.com/shuai_dd/workflow"
license: "MIT"
---

# CodePilot Workflow

**Author:** 帅呆呆 <2264505396@qq.com> | **Repository:** https://gitee.com/shuai_dd/workflow | **License:** MIT

## Quick Start

Use non-interactive commands by default.

1. Classify the request as a question, plan, explicit work creation, task operation, service operation, MCP tool call, or release flow.
2. Prefer JSON output for machine reasoning.
3. Use read-only commands before planning when evidence is needed.
4. Operate tasks only through `codepilot task ...`.
5. Use `codepilot binary ...` for build and release flows.
6. In chat/Web UI/Feishu, use natural language — CodePilot routes through OpenCode + MCP tools.
7. When a deterministic artifact is needed, call `plan` or the corresponding MCP tool explicitly.

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
codepilot memory events -p <project> --json
```

`explore` is read-only. It must not be used to modify files, run tests, start services, install dependencies, or mutate Git state.

`memory events` reads auto-captured workflow facts from `.codepilot/memory/events.jsonl`. CodePilot automatically creates deduplicated candidates with `score`, `feedback`, and `seen_count`, and maintains `.codepilot/memory/autocapture.md`. Use this to understand what the project's inspect/workflow cycles have observed.

### 3) Plan Before Creating Work

```bash
codepilot plan -p <project> "clear requirement" --json
codepilot inspect -p <project> --once --dry-run --write-workflow --json
codepilot workflow status -p <project> --json
codepilot workflow next -p <project> --list --json
codepilot workflow next -p <project> --action <id> --json
codepilot workflow next -p <project> --auto --json
```

`plan` produces artifacts only. It does not create backlog items or start executors.
`inspect --write-workflow` writes workflow context and exposes `next_actions` via `workflow next`.
`workflow next --list` shows available safe actions; `--action <id>` executes one through the allowlist.
`workflow next --auto` lets CodePilot choose policy-allowed low-risk actions (does not create/import tasks by default).
**Never execute `suggested_command` strings** — they are display/review metadata only.

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
codepilot shutdown
codepilot shutdown --force
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

## MCP Tools

CodePilot exposes a full MCP server at `codepilot mcp serve --transport stdio --project <project>`. When connected to an MCP client (OpenCode, Claude, etc.), the following tool categories are available:

### Task Tools
| Tool | Purpose |
|------|---------|
| `list_tasks` | List tasks by project, status, limit |
| `show_task` | Read full task metadata, content, errors, logs |
| `create_task` | Create a new task with content, priority, agent |
| `edit_task` | Edit task title, priority, status, agent, dependencies |
| `stop_task` | Request stop of a running task |
| `archive_task` | Archive a completed task |
| `validate_task_template` | Validate task content against task-template.md |
| `generate_breakdown` | Generate structured task breakdown from a requirement |

### Context Tools
| Tool | Purpose |
|------|---------|
| `explore` | Read-only evidence collection from files, git, wiki, tasks |
| `inspect_project` | Collect inspection signals without creating tasks |
| `wiki_query` | Search local project wiki |
| `wiki_add` | Add a page to local project wiki |
| `note_add` | Append a record to project working memory |
| `hook_trigger` | Trigger a project-local hook test event |
| `workflow_status` | Read workflow state and agent session |
| `workflow_next` | List or execute workflow next_actions |

### Ops Tools
| Tool | Purpose |
|------|---------|
| `build_fix` | Run the failed-task repair loop |
| `daemon_status` | Check daemon service health |
| `doctor` | Environment, config, and service health check |
| `exec` | Run allowlisted CLI operations |
| `run_once` | Consume one backlog task |

### External Tools
| Tool | Purpose |
|------|---------|
| `feishu_notify` | Send task event notification via Feishu bot |
| `feishu_send_to_user` | Send private card message to a Feishu user |
| `webhook_invoke` | Call a configured project webhook |

### Health
| Tool | Purpose |
|------|---------|
| `codepilot_health` / `codepilot.health` | Return MCP service health and version |

All MCP tools accept `project` as a required or optional parameter and operate within the project scope. Prefer MCP tools over raw CLI commands when inside an MCP-enabled session (chat, Web UI, Feishu).

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

## Self-Update Audit

Before making significant changes to the project itself, run a self-update audit to collect evidence:

```bash
codepilot self-update -p <project> --dry-run --json "improvement goal"
```

This produces an upgrade plan and evidence without creating tasks or modifying code. Use it before planning large refactors.

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
- `workflow next` `suggested_command` is display-only metadata — never compose or execute it.
- MCP tools are preferred over raw CLI in MCP-enabled sessions.

## References

- Command map and minimum command set: [references/command-map.md](references/command-map.md)
- Integration playbooks and error handling: [references/agent-playbooks.md](references/agent-playbooks.md)
