# CodePilot

Language: [中文](README.md) | English

CodePilot is a local engineering workflow CLI that turns natural-language requirements into executable tasks and connects planning, execution, review, inspection, service operations, and release workflows.

Current version: `0.7.4` | Author: [帅呆呆](https://gitee.com/shuai_dd) | Gitee: [shuai_dd/workflow](https://gitee.com/shuai_dd/workflow)

## Quick Start

Prepare a repository:

```bash
codepilot setup .
codepilot doctor --project <project-name> --services
```

Submit a requirement and let CodePilot decide whether to split and execute it:

```bash
codepilot "implement automatic planning and execution workflow"
codepilot go "fix task retry logic and add tests" -p <project-name>
```

Ask about project state, task totals, or service health:

```bash
codepilot go "how many tasks are complete in this project?" -p <project-name>
codepilot status -p <project-name> -v
codepilot hud -p <project-name> --preset full
```

`chat`, Web UI sessions, and Feishu free text all route through OpenCode + CodePilot MCP. Use natural language for questions, requirements, or task operations. Use `clarify` / `plan` / Web UI panels when a deterministic spec or plan artifact is needed.

## Common Commands

### Project And Requirements

```bash
codepilot init .
codepilot setup . --dry-run --json
codepilot clarify -p <project-name> "vague requirement" --json
codepilot plan -p <project-name> "clear requirement" --json
codepilot auto -p <project-name> -t "high-level goal" --plan-only
```

### Read-Only Context And Memory

```bash
codepilot explore -p <project-name> --prompt "question to investigate" --json
codepilot wiki query -p <project-name> "build" --json
codepilot note add -p <project-name> "current validation command is pytest tests"
codepilot trace -p <project-name> --limit 30
```

### Task Operations

```bash
codepilot task show <task_id>
codepilot task logs <task_id> --tail 80
codepilot task find <keyword> -p <project-name>
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task rm <task_id>
```

### Configuration

```bash
codepilot config init
codepilot config validate
codepilot config validate --fix
codepilot config sync
codepilot config sync --dry-run
```

### Queue, Services, And Troubleshooting

```bash
codepilot run -p <project-name> --once
codepilot daemon -p <project-name>
codepilot inspect -p <project-name> --once
codepilot build-fix -p <project-name> --task-id <task_id> --json
codepilot doctor --project <project-name> --services --json
```

### Web UI, Feishu, Webhook

```bash
codepilot ui
codepilot ui start
codepilot ui logs --tail 100
codepilot feishu start
codepilot feishu status
codepilot webhook --host 127.0.0.1 --port 8765
```

### AI, Event, Hook, Provider, Skill

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai template --format json
codepilot event schema --json
codepilot hook validate -p <project-name> --json
codepilot exec -p <project-name> --provider codex --dry-run --json -- codex --version
codepilot skill list -p <project-name> --json
```

### Binary Build And Release

```bash
codepilot binary build
codepilot binary install --binary <path-to-binary>
codepilot binary prepare --version <version>
codepilot binary release --build-current
codepilot binary verify
codepilot binary where
```

## CLI Agent Families And Fallbacks

CodePilot models callable CLI agents as families. The built-in families are `claude`, `codex`, and `opencode`. When one family is unavailable, CodePilot follows `[automation] fallback_cli_order`. OpenCode is treated as an upgradable interactive runtime; CodePilot injects providers, model selection, MCP, and permissions before launch.

Relevant `AGENTS.toml` fields:

```toml
[agents]
planner = ""
builder = ""
reviewer = ""

[agents.commands]
claude = "claude"
codex = "codex"
opencode = "opencode"

[automation]
agent_language = "en"
preflight_dirty_worktree = "stop"
fallback_cli_order = ["claude", "codex", "opencode"]
```

Legacy `[agents] codex_cmd / claude_cmd` scalar fields are removed. Run `codepilot config sync -p <project-name>` to rewrite old files.

## OpenCode Mode

`codepilot chat -a opencode` starts the official OpenCode binary and generates runtime files under `~/.codepilot/opencode/<project-id>/`:

- `opencode.json`: CodePilot MCP, default agent, commands, instructions, permissions, tools.
- `tui.json`: theme, scroll, diff, mouse, and CodePilot TUI branding plugin.
- `config/`: native OpenCode agent, command, instruction, and plugin files.

OpenCode branding, TUI integration, interaction-language rules, default agent, and built-in commands are CodePilot tool-level configuration. Business projects only need model providers and permission policy.

## Deprecated Entrypoints

Do not use these old entrypoints:

- `codepilot release ...`
- top-level `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- legacy `codepilot chat --no-ui` REPL
- `add --no-ai` / `add --allow-empty`

Use these replacements:

- Release: `codepilot binary ...`
- Tasks: `codepilot task ...`
- Web UI: `codepilot ui <start|status|logs|stop|restart>`
- Chat: `codepilot chat -a opencode`
- External task submission: read `codepilot ai template --format json`, then call `codepilot add ...` with template-compliant content.

## Documentation

- Overview: [中文](docs/说明文档.zh-CN.md) / [English](docs/说明文档.en-US.md)
- Operation guide: [中文](docs/操作文档.zh-CN.md) / [English](docs/操作文档.en-US.md)
- AI / Agent guide: [中文](docs/AI与Agent调用手册.zh-CN.md) / [English](docs/AI与Agent调用手册.en-US.md)
- Skill integration guide: [中文](docs/Skill化集成指南.zh-CN.md) / [English](docs/Skill化集成指南.en-US.md)
- Project services: [中文](docs/project-services.md) / [English](docs/project-services.en-US.md)
- Workflow state conventions: [中文](docs/workflow-state.zh-CN.md) / [English](docs/workflow-state.en-US.md)
- Static AI usage guide: [中文](AI_USAGE.zh-CN.md) / [English](AI_USAGE.en-US.md)

## Standard Entrypoints For Other AI Agents

Machine-readable command manifest:

```bash
codepilot ai manifest
```

Markdown usage guide:

```bash
codepilot ai guide
```

Short prompt:

```bash
codepilot ai prompt
```

Static artifacts are also kept at the repository root:

- `AI_MANIFEST.json`
- `AI_USAGE.zh-CN.md`
- `AI_USAGE.en-US.md`

## Skill Package

This repository includes a reusable Skill:

- `skills/codepilot-workflow/SKILL.md`

The Skill itself is written in English for other Codex / Agent runtimes. Installation and maintenance notes are in the Skill integration guide: [中文](docs/Skill化集成指南.zh-CN.md) / [English](docs/Skill化集成指南.en-US.md).
