# CodePilot Quickstart Guide

Language: [中文](01-快速上手指南.zh-CN.md) | English

A step-by-step guide to CodePilot — from installation to daily workflow mastery.

---

## Table of Contents

1. [What is CodePilot](#1-what-is-codepilot)
2. [Installation](#2-installation)
3. [First-Time Setup](#3-first-time-setup)
4. [Your First Task](#4-your-first-task)
5. [Daily Workflow](#5-daily-workflow)
6. [Configuration Guide](#6-configuration-guide)
7. [Common Scenarios](#7-common-scenarios)
8. [Web UI & Feishu](#8-web-ui--feishu)
9. [Advanced Features](#9-advanced-features)
10. [FAQ](#10-faq)

---

## 1. What is CodePilot

CodePilot is a CLI tool that turns natural language into executable development tasks, connecting the full pipeline:

```
You say "implement user login" → clarifies requirements → makes a plan → writes code → runs tests → reviews → reports
```

**Core Capabilities:**

- Describe requirements in plain language; CodePilot splits and executes them
- Built-in planner, builder, and reviewer roles working together
- Three AI engines (Claude, Codex, OpenCode) with automatic fallback
- Web UI dashboard, Feishu bot, and Webhook integrations
- Scheduled inspections, auto-fix, and project health checks

---

## 2. Installation

> **Platform Note**: CodePilot is tested on **Windows** and **Linux** only. **macOS has NOT been thoroughly tested** and may have significant bugs. macOS users are advised to use a VM or Docker, or wait for future macOS support.

### Option A: pip

```bash
pip install codepilot
```

### Option B: Binary

Download from [Releases](https://gitee.com/shuai_dd/CodePilot/releases):

```bash
codepilot binary install --binary ./codepilot-windows-amd64.exe
```

### Verify

```bash
codepilot --version
# codepilot, version 0.7.4
```

---

## 3. First-Time Setup

### 3.1 Initialize a Project

```bash
cd /path/to/your/project
codepilot init .
codepilot setup .
codepilot doctor --project <project-name> --services
```

### 3.2 Configure an AI Provider

Edit `AGENTS.toml` in your project root:

```toml
[providers.deepseek]
enabled = true
api_key = "sk-xxxxxxxxxxxxxxxx"
base_url = "https://api.deepseek.com"
complex_model = "deepseek-v4-pro"
simple_model = "deepseek-v4-flash"
```

Store secrets separately in `~/.codepilot.secrets.toml` to avoid committing them.

### 3.3 Interactive Setup

```bash
codepilot config init          # project-level
codepilot config init --global # global defaults
codepilot config validate      # verify configuration
```

---

## 4. Your First Task

### 4.1 Submit a Requirement

```bash
codepilot "add an installation section to README.md"
```

CodePilot will automatically: analyze → split → plan → execute → review.

### 4.2 Check Results

```bash
codepilot status -p <project-name> -v
codepilot trace -p <project-name> --limit 10
codepilot task show <task_id>
codepilot task logs <task_id> --tail 50
```

### 4.3 Conversational Mode

```bash
codepilot go "how many tasks are done in this project?" -p <project-name>
codepilot go "fix the task retry logic and add tests" -p <project-name>
```

---

## 5. Daily Workflow

### 5.1 Standard Flow: Clarify → Plan → Execute

```bash
# Step 1: Clarify requirements
codepilot clarify -p myproject "support phone OTP login" --json

# Step 2: Generate an execution plan (no code changes yet)
codepilot plan -p myproject "implement phone OTP login" --json

# Step 3: Auto-execute after reviewing the plan
codepilot auto -p myproject -t "implement phone OTP login"
```

### 5.2 Exploration

```bash
codepilot explore -p myproject --prompt "how is routing organized?" --json
codepilot wiki query -p myproject "build commands" --json
codepilot note add -p myproject "validation: pytest tests -q"
```

### 5.3 Code Review & Auto-Fix

```bash
codepilot "review the last 3 commits" -p myproject
codepilot build-fix -p myproject --task-id <task_id> --json
```

### 5.4 Task Management

```bash
codepilot task find "login" -p myproject
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
```

---

## 6. Configuration Guide

### 6.1 Provider (required)

```toml
[providers.deepseek]
enabled = true
api_key = "sk-xxx"
base_url = "https://api.deepseek.com"
complex_model = "deepseek-v4-pro"
simple_model = "deepseek-v4-flash"
```

### 6.2 Agents

```toml
[agents]
planner = ""
builder = ""
reviewer = ""

[agents.commands]
claude = "claude"
codex = "codex"
opencode = "opencode"
```

### 6.3 Automation

```toml
[automation]
agent_language = "en"                    # "en" or "zh-CN"
preflight_dirty_worktree = "stop"        # stop | commit | stash
fallback_cli_order = ["claude", "codex", "opencode"]
```

### 6.4 Scheduled Agents

```toml
[automation.scheduled_agents.task_health]
enabled = true
agent = "codex"
interval = "10m"
prompt = "Review task status and summarize risks."
max_cost_usd = 0.10
max_daily_cost_usd = 0.50
```

```bash
codepilot scheduled list -p myproject
codepilot scheduled run-once task_health -p myproject --dry-run
```

---

## 7. Common Scenarios

### New Feature

```bash
codepilot "implement CRUD API for shipping addresses"

# Check split tasks
codepilot task find "shipping" -p myproject --json

# Review results
codepilot task logs <task_id> --tail 50
```

### Bug Fix

```bash
codepilot go "fix order amount precision by replacing float with Decimal" -p myproject

# If auto-fix is insufficient:
codepilot build-fix -p myproject --task-id <task_id> \
  --verify-command "pytest tests/test_order.py -q" --json
```

### Project Inspection

```bash
codepilot inspect -p myproject              # start background service
codepilot inspect -p myproject --status      # check report
codepilot doctor --project myproject --services --json
```

### Multi-Project Management

```bash
codepilot status                             # list all projects
codepilot status -p project-a -v
codepilot status -p project-b -v
```

---

## 8. Web UI & Feishu

### Web UI

```bash
codepilot ui                          # start and open browser
codepilot ui start                    # background start
codepilot ui status                   # check status
codepilot ui restart                  # restart
```

Default: `http://localhost:8765`. Override with `CODEPILOT_WEBUI_PORT`.

### Feishu Bot

```toml
[feishu_bot]
enabled = true
app_id = "cli_xxx"
app_secret = ""
default_project = "myproject"
```

```bash
npm install                           # first time only
codepilot feishu start
```

---

## 9. Advanced Features

### Hooks

```bash
codepilot hook plan -p myproject --json
codepilot hook validate -p myproject --json
```

### Webhook

```bash
codepilot webhook --host 127.0.0.1 --port 8765
```

`POST /tasks`:

```json
{
  "project": "myproject",
  "title": "Fix build from CI",
  "content": "CI failure: test_login failed",
  "priority": "P1",
  "agent": "codex"
}
```

### Skills

```bash
codepilot skill list -p myproject --json
codepilot skill search "code review" -p myproject --json
codepilot skill enable build-fix -p myproject
```

### Release

```bash
codepilot binary build
codepilot binary prepare --version 0.8.0
codepilot binary verify
codepilot binary release --build-current
```

---

## 10. FAQ

### "Command not found"

Old commands have moved:

| Old | New |
|-----|-----|
| `codepilot show <id>` | `codepilot task show <id>` |
| `codepilot logs <id>` | `codepilot task logs <id>` |
| `codepilot webui ...` | `codepilot ui ...` |
| `codepilot release ...` | `codepilot binary ...` |

### Tasks stuck

```bash
codepilot status -p <project-name> -v
codepilot daemon -p <project-name> --status
codepilot run -p <project-name> --once
```

### API Key errors

1. Verify `enabled = true` for the provider
2. Check the key hasn't expired
3. Check `base_url` format
4. Test connectivity: `codepilot exec -p <project-name> --provider deepseek --dry-run --json -- codex --version`

### Environment isolation

```bash
export CODEPILOT_HOME="$HOME/.codepilot-dev"
```

---

## Next Steps

- [Overview](02-overview.en-US.md) — architecture and command model
- [Operations Guide](03-operation-guide.en-US.md) — full command reference
- [AI/Agent Guide](04-ai-agent-manual.en-US.md) — for other AI systems
- [Skill Integration Guide](05-skill-integration-guide.en-US.md) — writing and installing skills

Found an issue? [Open one here](https://gitee.com/shuai_dd/CodePilot/issues).
