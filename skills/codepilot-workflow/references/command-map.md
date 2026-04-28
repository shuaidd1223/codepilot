# CodePilot Command Map

This reference is for agents using the `codepilot-workflow` skill. Prefer non-interactive commands and JSON output whenever possible.

## 1. Natural-Language Entry Points

```bash
codepilot "<requirement>"
codepilot go "<requirement-or-question>"
codepilot chat
```

Use `codepilot "<requirement>"` or `codepilot go "<requirement>"` for one-shot requirement intake. `go` can also answer project and task status questions.

In `chat`, Web UI sessions, and Feishu free text, CodePilot uses a conservative execution boundary: ambiguous requirement-like text only returns a confirmation prompt and does not create work. Use explicit symbolic prefixes:

```text
? What is the current project status?
# Improve the Feishu task panel
! Retry failed task 12
```

## 2. Status Queries And Runtime Data

```bash
codepilot status -p <project> --json
codepilot task show <task_id> --json
codepilot task find <keyword> -p <project> --json
codepilot doctor --json
```

Question-style requests should use reliable local data first:

- Project list: `codepilot go "What projects are registered?"`
- Current project status: `codepilot go "What is the current project status?" -p <project>`
- Task totals: `codepilot go "How many tasks exist and how many are done?" -p <project>`
- Running or failed tasks: `codepilot go "Which tasks are currently running?" -p <project>`

## 3. Task Operations

```bash
codepilot task logs <task_id> --tail 80
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

## 4. Queue Execution And Services

```bash
codepilot run -p <project>
codepilot daemon -p <project>
codepilot daemon -p <project> --status
codepilot daemon -p <project> --stop
codepilot inspect -p <project> --once
codepilot inspect -p <project> --status
codepilot inspect -p <project> --stop
codepilot ui status
codepilot ui start
codepilot ui logs --tail 100
codepilot ui stop
```

## 5. Feishu And Webhook

```bash
codepilot feishu start
codepilot feishu status
codepilot feishu logs --tail 100
codepilot feishu stop
codepilot feishu run
codepilot webhook --host 127.0.0.1 --port 8765
```

- The Feishu long-connection worker deduplicates by `event_id` first, then `message_id`.
- Plain Feishu replies are sent as rich-text `post` messages; status, task, and event notifications use interactive cards.
- `codepilot feishu handle-event` is an internal JSON entry point. The worker parses JSON from the last stdout line, and Python-side noise is redirected to stderr.
- Feishu webhook notifications use interactive cards and keep `webhook_secret` signing support.

## 6. Binary Build And Release

```bash
codepilot binary build
codepilot binary install --binary <binary-path>
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <version>
codepilot binary where
```

## 7. AI Integration

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
codepilot ai template
codepilot ai template --format json
codepilot ai template --format guide
```

## 8. Removed Or Forbidden Forms

- `codepilot release ...`
- Top-level `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- Empty-task placeholders such as `--no-ai` / `--allow-empty`
