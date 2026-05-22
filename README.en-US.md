# CodePilot

<p align="center">
  <img src="docs/codepilot-logo.png" alt="CodePilot Logo" width="180">
</p>

<p align="center">
  <strong>Local Engineering Workflow CLI — Natural Language Driven Development</strong>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="CodePilot Demo" width="720">
</p>

<p align="center">
  <a href="https://gitee.com/shuai_dd/CodePilot"><img src="https://img.shields.io/badge/Gitee-Repo-red" alt="Gitee"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License"></a>
  <a href="#"><img src="https://img.shields.io/badge/version-0.7.4-green" alt="version"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey" alt="platform"></a>
  <a href="#"><img src="https://img.shields.io/badge/macOS-untested-orange" alt="macOS"></a>
</p>

> **Platform Support**: Tested on Windows and Linux only. macOS has not been thoroughly tested and may have significant bugs. Community contributions for macOS support are welcome.

Language: [中文](README.md) | English

CodePilot is a local engineering workflow CLI that turns natural-language requirements into executable tasks and connects planning, execution, review, inspection, service operations, and release workflows.

Current version: `0.7.4` | Author: [帅呆呆](https://gitee.com/shuai_dd) | Gitee: [shuai_dd/CodePilot](https://gitee.com/shuai_dd/CodePilot)

## Demo

### Screen Recording

<!-- Recording guide: Use ScreenToGif or LICEcap to capture terminal operations, export as docs/demo.gif -->

![demo](docs/demo.gif)

*Demo: Complete workflow from natural language requirement to automated execution.*

### Screenshots

<p align="center">
  <img src="docs/screenshot-webui.png" alt="Web UI" width="400">
  <img src="docs/screenshot-task.png" alt="Task Management" width="400">
</p>

*Left: Web UI Dashboard | Right: Task Status and Operations Panel*

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

## Typical Workflows

### Requirement → Task → Execution

```bash
# 1. Clarify vague requirements
codepilot clarify -p myproject "support phone OTP login" --json

# 2. Generate execution plan
codepilot plan -p myproject "implement phone OTP login" --json

# 3. Auto-execute (code + test + review)
codepilot auto -p myproject -t "implement phone OTP login"

# 4. Check execution results
codepilot task show <task_id>
codepilot task logs <task_id> --tail 50
```

### Code Review & Auto-Fix

```bash
# Submit code review
codepilot "review the latest commits" -p myproject

# Auto-fix build errors
codepilot build-fix -p myproject --task-id <task_id> --json
```

### Project Inspection

```bash
# Start background inspection service
codepilot inspect -p myproject

# Check inspection status
codepilot inspect -p myproject --status

# One-shot full check
codepilot inspect -p myproject --once
codepilot inspect -p myproject --once --dry-run --write-workflow --json
codepilot doctor --project myproject --services --json
```

`--write-workflow` creates inspection workflow context and safe `next_actions`, which can be reviewed with `codepilot workflow next -p myproject --list --json`, or advanced by one low-risk policy action with `codepilot workflow next -p myproject --auto --json`.

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
codepilot memory events -p <project-name> --json
codepilot trace -p <project-name> --limit 30
```

`memory events` reads the project-local automatic observation log and turns workflow actions plus task success/failure outcomes into deduplicated candidates with `score`, `feedback`, and `seen_count`.

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
codepilot inspect -p <project-name> --once --dry-run --write-workflow --json
codepilot build-fix -p <project-name> --task-id <task_id> --json
codepilot doctor --project <project-name> --services --json
```

### Scheduled / Event Agents

```bash
codepilot scheduled list -p <project-name>
codepilot scheduled show task_health -p <project-name> --json
codepilot scheduled run-once task_health --dry-run
codepilot scheduled disable task_health -p <project-name>
```

Minimal configuration:

```toml
[automation.scheduled_agents.task_health]
enabled = true
agent = "codex"
interval = "10m"
prompt = "Review local CodePilot task status and summarize risks."
max_cost_usd = 0.10
max_daily_cost_usd = 0.50

[automation.event_agents.failed_task_triage]
enabled = false
trigger = "task.failed"
agent = "codex"
prompt = "Task {{ task_id }} failed with {{ error_message }}. Suggest the smallest repair."
max_cost_usd = 0.10
max_daily_cost_usd = 0.50
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

- **Quickstart: [中文](docs/快速上手指南.zh-CN.md) / [English](docs/quickstart-guide.en-US.md)**
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

## License

This project is open sourced under the [MIT License](LICENSE).
