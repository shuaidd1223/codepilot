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

## 4. Status Queries And Runtime Data

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

## 5. Wiki, Notes, And Local Memory

```bash
codepilot wiki query -p <project> "keyword" --json
codepilot wiki add -p <project> --title "Build command" --body "pytest tests"
codepilot wiki ingest --from trace -p <project> --json
codepilot wiki lint -p <project> --json
codepilot note add -p <project> "short memory"
codepilot note show -p <project> --json
```

Do not write secrets, tokens, passwords, Feishu app secrets, or large temporary logs to wiki or notes.

## 6. Task Operations

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

## 7. Queue Execution And Services

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
```

## 8. UI, Feishu, And Webhook

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

## 9. Events, Hooks, Providers, And Skills

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

## 10. Binary Build And Release

```bash
codepilot binary build
codepilot binary install --binary <binary-path>
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <version>
codepilot binary where
```

## 11. AI Integration

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
codepilot ai template
codepilot ai template --format json
codepilot ai template --format guide
```

## 12. Direct Task Injection

```bash
codepilot ai template --format json
codepilot add -p <project> -t "task title"
codepilot add -p <project> -f tasks.json
codepilot add -p <project> -f tasks.md
codepilot add -p <project> -f tasks.txt
```

Use `add` only when directly injecting already planned work. `tasks.json` and `tasks.md` must contain complete task-template compliant content.

## 13. Removed Or Forbidden Forms

- `codepilot release ...`
- Top-level `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- Empty-task placeholders such as `--no-ai` and `--allow-empty`
