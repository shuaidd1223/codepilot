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
3. Only create tasks or submit the requirement after the user clearly wants execution.

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

## Playbook G: Operate Feishu Or Webhook Integrations

1. Configure Feishu long-connection credentials in `AGENTS.toml` and secrets in `.codepilot.secrets.toml`.
2. Start Feishu with `codepilot feishu start`.
3. Check `codepilot feishu status` and `codepilot feishu logs --tail 100` when diagnosing delivery or handler failures.
4. For HTTP task intake, run `codepilot webhook --host 127.0.0.1 --port 8765` and POST to `/tasks`.
5. For Feishu robot webhook notifications, configure provider `feishu`; cards are sent as interactive payloads with optional signing.

## Playbook H: Use Local Wiki, Notes, And Trace

1. Query durable facts with `codepilot wiki query -p <project> "<keyword>" --json`.
2. Add stable project facts with `codepilot wiki add -p <project> --title "<title>" --body "<body>"`.
3. Read short-term memory with `codepilot note show -p <project> --json`.
4. Add concise working memory with `codepilot note add -p <project> "<memory>"`.
5. Diagnose recent activity with `codepilot trace -p <project> --limit 30 --json`.
6. Never store secrets, credentials, or large raw logs in wiki or notes.

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

## Error Handling Rules

- If the output says a command was removed, switch to the `task`, `binary`, or `ui` command group.
- If a machine-readable result is needed, add `--json`.
- If a JSON envelope contains `error.code`, branch on `error.code` before retrying blindly.
- Do not use empty task placeholders; external agents must provide complete task-template content or let CodePilot generate it through `add -t`.
- If a command is meant to be read-only, prefer `explore`, `plan`, `status`, `hud`, `trace`, `wiki query`, or `doctor` over execution commands.
