# Agent Playbooks

## Playbook A: Submit A Requirement And Track It

1. Run `codepilot "<requirement>"`.
2. Read `codepilot status -p <project> --json`.
3. Locate task IDs from `tasks[].id`.
4. Read details with `codepilot task show <task_id> --json`.
5. Follow progress with `codepilot task logs <task_id> --tail 80`.

## Playbook B: Answer A Project Or Task Question

1. Treat status, totals, completion, failed tasks, running tasks, service state, and project-list requests as questions.
2. Prefer direct structured commands when the answer must be deterministic:
   - `codepilot status -p <project> --json`
   - `codepilot task find <keyword> -p <project> --json`
   - `codepilot daemon -p <project> --status`
   - `codepilot inspect -p <project> --status`
3. If natural-language handling is desired, use `codepilot go "<question>" -p <project>`.
4. Do not create tasks from exploratory or ambiguous wording.

## Playbook C: Work Safely In Chat, Web UI, Or Feishu

1. Use plain text or `?` for questions.
2. Use `# <content>` to create a requirement that may need planning.
3. Use `! <content>` to create a small concrete task.
4. If CodePilot returns a confirmation prompt, repeat the request with `#` or `!` only if the user clearly wants work to be created.
5. For Feishu project context, use commands like `projects`, `use <project>`, `status <project>`, `tasks <project>`, `detail <id>`, `retry <id>`, and `stop <id>`.

## Playbook D: Recover A Failed Task

1. Read `codepilot task logs <task_id> --full`.
2. Decide whether the failure is caused by environment, permissions, credentials, dirty workspace, or code behavior.
3. Stop the task first if it is still running: `codepilot task stop <task_id>`.
4. Requeue with `codepilot task retry <task_id>`.
5. Execute with `codepilot run -p <project>` or let the project daemon pick it up.

## Playbook E: Keep A Project Running In The Background

1. Start queue polling with `codepilot daemon -p <project>`.
2. Monitor with `codepilot daemon -p <project> --status`.
3. Stop polling gracefully with `codepilot daemon -p <project> --stop`; this stops picking up new tasks after the current one finishes.
4. If signal inspection is needed, use `codepilot inspect -p <project> --once` or the inspect background service.

## Playbook F: Operate Feishu Or Webhook Integrations

1. Configure Feishu long-connection credentials in `AGENTS.toml` and secrets in `.codepilot.secrets.toml`.
2. Start Feishu with `codepilot feishu start`.
3. Check `codepilot feishu status` and `codepilot feishu logs --tail 100` when diagnosing delivery or handler failures.
4. For HTTP task intake, run `codepilot webhook --host 127.0.0.1 --port 8765` and POST to `/tasks`.
5. For Feishu robot webhook notifications, configure provider `feishu`; cards are sent as interactive payloads with optional signing.

## Playbook G: Prepare A Release

1. Run `codepilot binary prepare --version <version>`.
2. Verify with `codepilot binary verify`.
3. If only packaging current artifacts is needed, run `codepilot binary release --build-current`.

## Error Handling Rules

- If the output says a command was removed, switch to the `task`, `binary`, or `ui` command group.
- If a machine-readable result is needed, add `--json`.
- If a JSON envelope contains `error.code`, branch on `error.code` before retrying blindly.
- Do not use empty task placeholders; external agents must provide complete task-template content or let CodePilot generate it through `add -t`.
