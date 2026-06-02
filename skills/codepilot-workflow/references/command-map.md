# CodePilot Command Map

This reference is for agents using the `codepilot-workflow` skill. Prefer non-interactive commands and JSON output whenever possible.

## 1. Project Setup

```bash
codepilot setup . --dry-run --json
codepilot setup .
codepilot init .
codepilot doctor --project <project> --services --json
```

Use `setup` for the current project-level `.codepilot` skeleton and registration flow. Use `init` only when basic project registration is enough.

## 2. Natural-Language Entry Points

```bash
codepilot "<requirement>"
codepilot go "<requirement-or-question>" -p <project>
codepilot chat -p <project> -a opencode
```

Use `codepilot "<requirement>"` or `codepilot go "<requirement>"` for one-shot requirement intake. `go` can also answer project and task status questions.

In `chat`, Web UI sessions, and Feishu free text, CodePilot routes free text into OpenCode + CodePilot MCP. Use plain natural language, or explicitly call `plan` / MCP tools when a deterministic artifact is required:

```text
What is the current project status?
Improve the Feishu task panel.
Before retrying failed task 12, list the risk and ask for confirmation if needed.
```

CodePilot-launched OpenCode is isolated from a user's native OpenCode install. Generated config, TUI plugin files, session data, and project model selection live under `~/.codepilot/opencode/<project-scope>/`.

## 3. Planning And Read-Only Evidence

```bash
codepilot plan -p <project> "clear requirement" --json
codepilot explore -p <project> --prompt "question or search terms" --json
```

- `plan` writes a reviewable plan artifact and does not start execution.
- `explore` is read-only and returns evidence, sources, and limitations.

## 4. Workflow State And Safe Advancement

```bash
codepilot inspect -p <project> --once --dry-run --write-workflow --json
codepilot workflow status -p <project> --json
codepilot workflow auto-policy -p <project> --json
codepilot workflow next -p <project> --list --json
codepilot workflow next -p <project> --action <id> --json
codepilot workflow next -p <project> --auto --json
```

- `inspect --write-workflow` collects signals, writes workflow context, and records `next_actions`.
- `workflow status` reads the current agent session, artifact paths, and auto_policy.
- `workflow auto-policy` returns resolved policy flags (allow_create_inspect_tasks, allow_import_plan_tasks, max_steps, failure_threshold).
- `workflow next --list` shows available safe actions with id, label, risk, and `suggested_command`.
- `workflow next --action <id>` executes one allowlisted action — the only safe way to advance.
- `workflow next --auto` lets CodePilot choose policy-allowed low-risk actions.
- **Never execute `suggested_command`** — it is display/review metadata, not an executable source.

## 5. Status Queries And Runtime Data

```bash
codepilot status -p <project> --json
codepilot hud -p <project> --preset full --json
codepilot trace -p <project> --limit 30 --json
codepilot task show <task_id> --json
codepilot task find <keyword> -p <project> --json
codepilot doctor --json
```

Question-style requests should use reliable local data first:

- Project status: `codepilot go "What is the current project status?" -p <project>`
- Task totals: `codepilot go "How many tasks exist and how many are done?" -p <project>`
- Running or failed tasks: `codepilot go "Which tasks are currently running?" -p <project>`
- Service health: `codepilot doctor --project <project> --services --json`

## 6. Wiki, Notes, Memory, And Local Knowledge

```bash
codepilot wiki query -p <project> "keyword" --json
codepilot wiki add -p <project> --title "Build command" --body "pytest tests"
codepilot wiki ingest --from trace -p <project> --json
codepilot wiki lint -p <project> --json
codepilot note add -p <project> "short memory"
codepilot note show -p <project> --json
codepilot memory events -p <project> --json
codepilot memory events -p <project> --type workflow.action_executed --json
```

- `wiki` is for durable, long-lived project knowledge (build commands, architecture decisions, failure modes).
- `note` is for persistent working memory across sessions.
- `memory events` reads auto-captured factual events from `.codepilot/memory/events.jsonl`. CodePilot automatically creates deduplicated candidates with `score`, `feedback`, and `seen_count`, and maintains `.codepilot/memory/autocapture.md`.
- Do not write secrets, tokens, passwords, Feishu app secrets, or large temporary logs to wiki, notes, or memory.

## 7. Task Operations

```bash
codepilot task logs <task_id> --tail 80
codepilot task logs <task_id> --full
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task done <task_id>
codepilot task edit <task_id> --status backlog
codepilot task rm <task_id>
codepilot task sweep <task_id>
```

## 8. Queue Execution And Services

```bash
codepilot run -p <project>
codepilot run -p <project> --once
codepilot daemon -p <project>
codepilot daemon -p <project> --status
codepilot daemon -p <project> --stop
codepilot inspect -p <project> --once --json
codepilot inspect -p <project> --status
codepilot inspect -p <project> --stop
codepilot build-fix -p <project> --task-id <task_id> --json
codepilot shutdown
codepilot shutdown -p <project>
codepilot shutdown --force
```

`shutdown` stops Web UI, Feishu, webhook, daemon, inspect, and active task runtime processes in one operation.

## 9. UI, Feishu, And Webhook

```bash
codepilot ui start
codepilot ui status
codepilot ui logs --tail 100
codepilot ui stop
codepilot feishu start
codepilot feishu status
codepilot feishu logs --tail 100
codepilot feishu stop
codepilot feishu run
codepilot webhook --host 127.0.0.1 --port 8765
```

- The Feishu long-connection worker deduplicates by `event_id` first, then `message_id`.
- Feishu task, status, event, and error messages use interactive cards.
- `codepilot feishu handle-event` is an internal JSON entry point.

## 10. Events, Hooks, Providers, And Skills

```bash
codepilot event schema --json
codepilot event list -p <project> --json
codepilot event test -p <project> --event doctor.checked --json
codepilot hook validate -p <project> --json
codepilot hook test -p <project> --provider codex --event agent.prompt.submitted --json
codepilot exec -p <project> --provider codex --dry-run --json -- codex --version
codepilot skill list -p <project> --json
codepilot skill run ralplan -p <project> --provider codex --input "requirement" --json
```

Hook commands validate and test project-level wrappers. They do not modify global Codex, Claude, or Gemini hook files.

## 11. Binary Build And Release

```bash
codepilot binary build
codepilot binary install --binary <binary-path>
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <version>
codepilot binary where
```

## 12. AI Integration

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
codepilot ai template
codepilot ai template --format json
codepilot ai template --format guide
```

## 13. Self-Update Audit

```bash
codepilot self-update -p <project> --dry-run --json "improvement goal"
```

Collects evidence and produces an upgrade plan without creating tasks or modifying code. Use before planning significant refactors or project-level improvements.

## 14. MCP Server

```bash
codepilot mcp serve --transport stdio --project <project>
```

Starts the CodePilot MCP server over stdio. This is the entry point used by MCP clients (OpenCode, Claude, etc.) to access CodePilot tools. The server exposes all built-in MCP tools across four categories:

- **Task tools:** list_tasks, show_task, create_task, edit_task, stop_task, archive_task, validate_task_template, generate_breakdown
- **Context tools:** explore, inspect_project, wiki_query, wiki_add, note_add, hook_trigger, workflow_status, workflow_next
- **Ops tools:** build_fix, daemon_status, doctor, exec, run_once
- **External tools:** feishu_notify, feishu_send_to_user, webhook_invoke
- **Health:** codepilot_health / codepilot.health

## 15. Direct Task Injection

```bash
codepilot ai template --format json
codepilot add -p <project> -t "task title"
codepilot add -p <project> -f tasks.json
codepilot add -p <project> -f tasks.md
codepilot add -p <project> -f tasks.txt
```

Use `add` only when directly injecting already planned work. `tasks.json` and `tasks.md` must contain complete task-template compliant content.

## 16. Removed Or Forbidden Forms

- `codepilot release ...`
- Top-level `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- Empty-task placeholders such as `--no-ai` and `--allow-empty`
- Executing `suggested_command` strings from `workflow next` output
