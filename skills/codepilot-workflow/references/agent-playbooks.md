# Agent Playbooks

## Playbook A: Submit A Requirement And Track It

1. Run `codepilot "<requirement>"`.
2. Read `codepilot status -p <project> --json`.
3. Locate task IDs from the status payload.
4. Read details with `codepilot task show <task_id> --json`.
5. Follow progress with `codepilot task logs <task_id> --tail 80`.

## Playbook B: Plan Before Execution

1. Run `codepilot plan -p <project> "<requirement>" --json`.
2. Review files, risks, order, and verification matrix from the plan payload.
3. Check `next_actions` in the output and advance with `codepilot workflow next -p <project> --list --json`.
4. Only create tasks or submit the requirement after the user clearly wants execution.
5. Use `codepilot workflow next -p <project> --action import_tasks --json` to import plan tasks when ready.

## Playbook C: Answer A Project Or Task Question

1. Treat status, totals, completion, failed tasks, running tasks, service state, and project-list requests as questions.
2. Prefer direct structured commands when the answer must be deterministic:
   - `codepilot status -p <project> --json`
   - `codepilot hud -p <project> --preset full --json`
   - `codepilot trace -p <project> --limit 30 --json`
   - `codepilot task find <keyword> -p <project> --json`
   - `codepilot doctor --project <project> --services --json`
3. If evidence is needed, use `codepilot explore -p <project> --prompt "<question>" --json`.
4. Do not create tasks from exploratory or ambiguous wording.

## Playbook D: Work Safely In Chat, Web UI, Or Feishu

1. Treat these channels as OpenCode + CodePilot MCP conversations.
2. Use plain natural language for questions, requirements, and operation requests.
3. If a deterministic artifact is required, call `codepilot plan` or the corresponding MCP tool explicitly.
4. For Feishu project context and explicit actions, use commands like `projects`, `use <project>`, `status <project>`, `tasks <project>`, `detail <id>`, `retry <id>`, and `stop <id>`.
5. Remember that CodePilot-launched OpenCode uses isolated runtime state under `~/.codepilot/opencode/<project-scope>/`.

## Playbook E: Recover A Failed Task

1. Read `codepilot task logs <task_id> --full`.
2. Decide whether the failure is caused by environment, permissions, credentials, dirty workspace, or code behavior.
3. Stop the task first if it is still running: `codepilot task stop <task_id>`.
4. If a full repair loop is appropriate, run `codepilot build-fix -p <project> --task-id <task_id> --dry-run`, then run the non-dry-run command.
5. If the issue only needs requeueing, run `codepilot task retry <task_id>` and then `codepilot run -p <project>`.

## Playbook F: Keep A Project Running In The Background

1. Start queue polling with `codepilot daemon -p <project>`.
2. Monitor with `codepilot daemon -p <project> --status`.
3. Stop polling gracefully with `codepilot daemon -p <project> --stop`.
4. If signal inspection is needed, use `codepilot inspect -p <project> --once --json` or the inspect background service.
5. Use `codepilot hud -p <project> --preset full --json` for a compact service and queue summary.
6. To stop all services at once, use `codepilot shutdown` or `codepilot shutdown --force`.

## Playbook G: Operate Feishu Or Webhook Integrations

1. Configure Feishu long-connection credentials in `AGENTS.toml` and secrets in `.codepilot.secrets.toml`.
2. Start Feishu with `codepilot feishu start`.
3. Check `codepilot feishu status` and `codepilot feishu logs --tail 100` when diagnosing delivery or handler failures.
4. For HTTP task intake, run `codepilot webhook --host 127.0.0.1 --port 8765` and POST to `/tasks`.
5. For Feishu robot webhook notifications, configure provider `feishu`; cards are sent as interactive payloads with optional signing.

## Playbook H: Use Local Wiki, Notes, Memory, And Trace

1. Query durable facts with `codepilot wiki query -p <project> "<keyword>" --json`.
2. Add stable project facts with `codepilot wiki add -p <project> --title "<title>" --body "<body>"`.
3. Read short-term memory with `codepilot note show -p <project> --json`.
4. Add concise working memory with `codepilot note add -p <project> "<memory>"`.
5. Diagnose recent activity with `codepilot trace -p <project> --limit 30 --json`.
6. Read auto-captured workflow facts with `codepilot memory events -p <project> --json`.
7. Never store secrets, credentials, or large raw logs in wiki, notes, or memory.

## Playbook I: Validate Events, Hooks, Providers, And Skills

1. Read event schemas with `codepilot event schema --json`.
2. Inspect sinks with `codepilot event list -p <project> --json`.
3. Validate hook registry with `codepilot hook validate -p <project> --json`.
4. Generate a lifecycle test event with `codepilot hook test -p <project> --provider codex --event agent.prompt.submitted --json`.
5. Smoke-test providers with `codepilot exec -p <project> --provider codex --dry-run --json -- codex --version`.
6. Inspect local workflow skills with `codepilot skill list -p <project> --json`.

## Playbook J: Prepare A Release

1. Run `codepilot binary prepare --version <version>`.
2. Verify with `codepilot binary verify`.
3. If only packaging current artifacts is needed, run `codepilot binary release --build-current`.

## Playbook K: Use MCP Tools Directly

When operating inside an MCP-enabled session (OpenCode chat, Web UI, Feishu), prefer MCP tools over raw CLI commands:

1. **For task queries:** Use `list_tasks`, `show_task` instead of `codepilot task show --json`.
2. **For task creation:** Use `create_task` with proper content, priority, agent fields.
3. **For project inspection:** Use `inspect_project` to collect signals, `explore` for read-only evidence.
4. **For workflow:** Use `workflow_status` to read state, `workflow_next` to list/execute actions.
5. **For repair:** Use `build_fix` for the full repair loop with verification.
6. **For knowledge:** Use `wiki_query`, `wiki_add`, `note_add` for persistent storage.
7. **For health:** Use `codepilot_health` to check MCP service status.
8. **For notifications:** Use `feishu_notify` or `feishu_send_to_user` from within task workflows.

All MCP tools accept `project` as a parameter and operate within the project scope. Tool schemas include descriptions and parameter types — let the MCP client discover them.

## Playbook L: Manage Memory And Autocapture

CodePilot automatically captures workflow facts into `.codepilot/memory/events.jsonl` and maintains deduplicated candidates:

1. Read recent memory events: `codepilot memory events -p <project> --json`.
2. Filter by event type: `codepilot memory events -p <project> --type workflow.action_executed --json`.
3. Review the autocapture summary: read `.codepilot/memory/autocapture.md`.
4. Each candidate has `score` (relevance), `feedback` (positive/negative/neutral), and `seen_count`.
5. Workflow actions and terminal task outcomes automatically adjust candidate weights.
6. Memory events are factual observations — they do not replace human-maintained wiki or notes.
7. Use memory events to understand what inspect/workflow cycles have observed before planning new work.

## Playbook M: Self-Update And Improve The Project

Before making significant changes to CodePilot's own project configuration or architecture:

1. Run `codepilot self-update -p <project> --dry-run --json "improvement goal"`.
2. Review the collected evidence, identified gaps, and proposed upgrade plan.
3. The dry run does not create tasks or modify code — it only produces a plan artifact.
4. If the plan looks correct, submit the improvement as a regular requirement: `codepilot "<improvement goal>"`.

## Error Handling Rules

- If the output says a command was removed, switch to the `task`, `binary`, or `ui` command group.
- If a machine-readable result is needed, add `--json`.
- If a JSON envelope contains `error.code`, branch on `error.code` before retrying blindly.
- Do not use empty task placeholders; external agents must provide complete task-template content or let CodePilot generate it through `add -t`.
- If a command is meant to be read-only, prefer `explore`, `plan`, `status`, `hud`, `trace`, `wiki query`, or `doctor` over execution commands.
- Never execute `suggested_command` strings from `workflow next` output — they are display/review metadata only.
- In MCP-enabled sessions, prefer MCP tools over raw CLI for task operations, context queries, and service checks.
