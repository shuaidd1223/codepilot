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

`chat`, Web UI sessions, and Feishu free text all route through OpenCode + CodePilot MCP. Use natural language for questions, requirements, or task operations. Use `plan` / Web UI panels when a deterministic plan artifact is needed.

## Typical Workflows

### Requirement → Task → Execution

```bash
# 1. Generate execution plan
codepilot plan -p myproject "implement phone OTP login" --json

# 2. Auto-execute (code + test + review)
codepilot auto -p myproject -t "implement phone OTP login"

# 3. Check execution results
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

`--write-workflow` creates inspection workflow context and safe `next_actions`, which can be reviewed with `codepilot workflow next -p myproject --list --json`, or advanced by low-risk policy actions with `codepilot workflow next -p myproject --auto --json`. `--auto` never executes the `suggested_command` string; by default it stays conservative and does not create inspect tasks or import plan tasks.

Project `AGENTS.toml` can opt in to broader automatic progress:

```toml
workflow_auto_create_inspect_tasks = false
workflow_auto_import_plan_tasks = false
workflow_auto_max_steps = 1
workflow_auto_failure_threshold = 1
```

## Common Commands

### Project And Requirements

```bash
codepilot init .
codepilot setup . --dry-run --json
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

## Project Architecture

### Business Scenarios

CodePilot covers the full lifecycle of local engineering workflows across 7 core scenarios:

| Scenario | Description | Primary Entrypoints |
| --- | --- | --- |
| **Requirements → Tasks** | Turn vague requirements into executable tasks, auto-plan and execute one by one | `codepilot go "..."`, `codepilot plan` |
| **Review Loop** | Dual-phase agent collaboration — builder implements, reviewer inspects; FAIL sends the task back to builder for repair, up to `max_review_rounds` | `codepilot auto`, `codepilot build-fix` |
| **Project Inspection** | Multi-dimensional signal collection: code scale & complexity, dependency health, TODO markers, failed tasks, git activity; produces reviewable candidate reports with workflow context writeback | `codepilot inspect --once`, `codepilot doctor` |
| **Multi-Channel** | CLI text mode, Web UI dashboard, Feishu/Lark bot bidirectional interaction, Webhook HTTP callbacks | `codepilot ui start`, `codepilot feishu start`, `codepilot webhook` |
| **Autonomous Agents** | Cron-scheduled or event-triggered (e.g. `task.failed`) agent jobs with per-job/daily cost guards, circuit breakers, and audit logging | `codepilot scheduled list`, `codepilot scheduled run-once` |
| **Binary Release** | One-shot PyInstaller frozen binary builds; vendor CLI (codex/opencode) auto-fetch, verify, cache, and bundle | `codepilot binary build`, `codepilot binary release` |
| **Multi-AI Runtime** | Claude Code / Codex CLI / OpenCode — three CLI agent families with automatic fallback chains; OpenCode as an upgradable TUI interactive kernel | `codepilot chat -a opencode`, `codepilot exec` |

### Module Map

```
codepilot/
├── cli.py                     # CLI entrypoint, natural-language routing, 50+ lazy-loading commands
├── commands/                  # Click subcommands (grouped by domain, ~70 files)
│   ├── auto.py                #   `auto`/`go` entry: requirement → task main pipeline
│   ├── auto_chat.py           #   Chat mode interaction pipeline (intent dispatch, session mgmt)
│   ├── auto_chat_commands.py  #   In-chat slash commands (/task, /inspect, /plan ...)
│   ├── auto_workflow.py       #   Natural-language workflow: project resolution → intent → plan → execute
│   ├── auto_workflow_planning.py # Planner dispatch (two-stage recon + breakdown)
│   ├── auto_project_resolution.py # Project resolution (by name / path / CWD auto-discovery)
│   ├── plan.py                #   Execution plan generation (spec → task breakdown → acceptance criteria)
│   ├── run.py                 #   Task execution entry (CLI command + shared helpers)
│   ├── run_orchestrator.py    #   Queue orchestration (context resolution, workspace prep, executor dispatch)
│   ├── run_builtin.py         #   Builtin executor CLI entry
│   ├── run_builtin_core.py    #   Builtin executor shared utilities (agent resolution, preflight, runtime)
│   ├── run_builtin_executor.py #  Builtin dual-phase execution engine (builder + reviewer loop)
│   ├── run_builtin_prompts.py #   Builtin executor prompt construction
│   ├── run_live_runner.py     #   Live subprocess execution monitor (heartbeat, output filter, cancellation)
│   ├── run_git.py             #   Git isolation operations (branch/worktree creation, preflight checks)
│   ├── run_shell.py           #   Cross-platform shell command execution and detection
│   ├── run_failure_triage.py  #   Failure triage main module (re-exports)
│   ├── run_failure_triage_apply.py    # Triage decision execution (retry / replan / discard)
│   ├── run_failure_triage_decisions.py # Triage decision engine (evidence collection, decision mapping)
│   ├── run_failure_triage_prompts.py  # Triage prompt construction
│   ├── reviewer_output.py     #   Reviewer output parsing (PASS / FAIL verdict)
│   ├── inspect.py             #   Inspection CLI + signal collection dispatch
│   ├── inspect_service.py     #   Inspection background service lifecycle
│   ├── inspect_lifecycle.py   #   Inspection report lifecycle (promote / ignore / delete / archive)
│   ├── inspect_signals.py     #   Signal fingerprinting and grouping
│   ├── inspect_signal_collectors*.py # Signal collectors (code metrics / dependency health / TODOs)
│   ├── inspect_workflow.py    #   Inspection → workflow context writeback
│   ├── task.py                #   `task` command group entry
│   ├── tasks.py               #   Task CRUD command implementations
│   ├── task_quality.py        #   Task quality validation
│   ├── chat.py                #   `chat` interactive session (OpenCode kernel)
│   ├── config_cmd.py          #   `config init/validate/sync` configuration governance
│   ├── binary.py              #   `binary build/install/release/verify`
│   ├── doctor.py              #   Comprehensive project health diagnostics
│   ├── daemon.py              #   Task queue daemon
│   ├── scheduled.py           #   Scheduled / event agent management
│   ├── feishu.py              #   Feishu service control
│   ├── webui_service.py       #   Web UI background service (start / stop / restart / logs)
│   ├── hud.py                 #   Project dashboard (task stats / health / trends)
│   ├── status.py              #   Project status display
│   ├── explore.py             #   Read-only project exploration (code search / Q&A)
│   ├── wiki.py                #   Project knowledge base (ingest / query)
│   ├── note.py                #   Project note management
│   ├── trace.py               #   Audit tracing (operation history)
│   ├── memory.py              #   Memory events CLI
│   ├── setup.py               #   Project setup wizard
│   ├── init.py                #   Quick initialization
│   ├── cleanup.py             #   Expired data cleanup
│   ├── hook.py, event.py      #   Hook and event management
│   ├── exec_cmd.py            #   Pass-through external CLI agent execution
│   ├── skill.py               #   Skill package management
│   ├── add.py                 #   External task submission entrypoint
│   ├── self_update.py         #   Tool self-update
│   ├── mcp.py                 #   MCP debug commands
│   ├── requirement_worker.py  #   Requirement worker entry
│   └── ...
├── core/                      # Core infrastructure
│   ├── config.py              #   AGENTS.toml discovery, parsing, full data model (10+ config classes)
│   ├── config_builder.py      #   Config builder (from_dict, secrets overlay, provider merging)
│   ├── config_parse.py        #   Config parsing and validation (agent normalization, value checks)
│   ├── workflow_state.py      #   Workflow state machine (mode state / session / artifact JSON file persistence)
│   ├── runtime.py             #   Process management (spawn / heartbeat / kill), subprocess utils, Windows console suppression
│   ├── memory.py              #   Observation memory system (event append → dedup merge → scoring → candidate → autocapture.md)
│   ├── event_plugins.py       #   Event plugin bus (publish / subscribe / lifecycle)
│   ├── hook_registry.py       #   Hook registration and execution (pre/post task, pre/post phase)
│   ├── skill_catalog.py       #   Skill directory scanning, parsing, and validation
│   ├── progress_bus.py        #   Progress event bus (LLM heartbeat → SSE → Web UI real-time rendering)
│   ├── service_launcher.py    #   Background service detach launcher (cross-platform CREATE_NO_WINDOW)
│   ├── task_mutation_guard.py #   Task state mutation safety guard (state machine validity checks)
│   ├── task_template.py       #   Task template compliance validation
│   ├── web_events.py          #   Web UI SSE event encoding
│   ├── models.py              #   Shared data models (Task, Session, Project)
│   ├── logger.py              #   Structured logging (per-module levels)
│   ├── output.py              #   Terminal output formatting (Rich markup safe escaping)
│   ├── error_messages.py      #   Error message templates
│   ├── paths.py               #   Global / project storage path conventions
│   ├── console_encoding.py    #   Windows console encoding fix
│   ├── cli_progress.py        #   CLI progress bar components
│   ├── gitignore.py           #   .gitignore management
│   ├── text_decode.py         #   Subprocess output decoding
│   └── ...
├── ai_support/                # AI integration layer
│   ├── providers.py           #   CLI + API provider registry and lifecycle (CLI_PROVIDERS / API_PROVIDERS)
│   ├── provider_adapters.py   #   Provider adapters (OpenAI / Anthropic / DeepSeek SDK unified interface)
│   ├── provider_profiles.py   #   Provider capability descriptors (models, token limits, cost params)
│   ├── provider_registry.py   #   Provider lookup
│   ├── cli_families.py        #   CLI agent family registry (claude / codex / opencode + EnvBridge)
│   ├── family_runtime.py      #   Agent runtime environment (API key bridge, command resolution)
│   ├── gateway_options.py     #   AI gateway option aggregation
│   ├── intent_classifier.py   #   Intent classifier (requirement / question / command)
│   ├── intent_rules.py        #   Heuristic intent matching rules (Chinese keyword / sentence patterns)
│   ├── classifier.py          #   Task classifier (priority / type / complexity)
│   ├── task_planning.py       #   Task planning engine (spec → structured task list)
│   ├── planner_context.py     #   Planner context construction (project metadata / file tree / spec summary)
│   ├── planner_execution.py   #   Planner execution dispatch (schema-prompt / CLI fallback)
│   ├── planner_parse.py       #   Plan result parsing (JSON schema → task dict)
│   ├── main_execute.py        #   Main execution pipeline (plan → task creation → execution dispatch)
│   ├── main_resolution.py     #   Main resolution pipeline (requirement → agent → config → executor)
│   ├── interaction_controller.py # Interaction controller (chat session lifecycle)
│   ├── question_answering.py  #   Q&A handling (context retrieval → answer generation)
│   ├── question_runtime.py    #   Q&A runtime (OpenCode in-session Q&A)
│   ├── opencode_runtime.py    #   OpenCode runtime management (start / stop / config injection)
│   ├── agent_manifest.py      #   AI_MANIFEST.json generation
│   ├── agent_guides.py        #   AI_USAGE.md generation
│   ├── agent_commands.py      #   Machine-readable command listing
│   ├── agent_support.py       #   Agent support utilities (templates / validation / build)
│   ├── agent_task_template.py #   Task template validation (required fields / format)
│   ├── backlog_dedup.py       #   Backlog deduplication (title / file similarity)
│   ├── project_metadata.py    #   Project metadata extraction (languages / frameworks / dependencies)
│   ├── prompts.py             #   Prompt template loading and rendering
│   ├── result_parse.py        #   Execution result parsing (exit code / output / summary)
│   └── service.py             #   AI service unified entrypoint (normalize_agent_name, etc.)
├── gateway/                   # AI API gateway
│   ├── api.py                 #   API call entrypoint (try_api → resolve → execute → format)
│   ├── resolution.py          #   Provider resolution and routing (config → provider key → adapter)
│   ├── execute.py             #   API execution (HTTP POST + SDK invoke)
│   ├── prompt_build.py        #   Prompt construction (system / user message assembly)
│   ├── entrypoints.py         #   Entrypoint registry (dispatcher: CLI vs API)
│   ├── call_skeleton.py       #   Call skeleton (request / response normalization)
│   ├── service.py             #   Gateway service
│   └── types.py               #   Gateway types (GatewayMode / GatewayRequest / GatewayResponse)
├── storage/                   # Persistence layer (SQLite)
│   ├── database.py            #   Database initialization, connection pool, caching, query functions
│   ├── schema_store.py        #   Schema version management (baseline + incremental migrations)
│   ├── task_write_store.py    #   Task CRUD (create / read / update / delete + state transitions)
│   ├── task_read_model.py     #   Task queries (pagination / filtering / sorting / stats)
│   ├── session_store.py       #   Session storage (message / session CRUD)
│   ├── project_store.py       #   Project registration (upsert / delete / fetch)
│   └── service_state_store.py #   Service state storage (PID / port / health)
├── mcp/                       # MCP (Model Context Protocol) server
│   ├── server.py              #   MCP server (FastMCP binding, tool registration, async execution)
│   ├── tool_registry.py       #   Tool registry (declarative ToolDefinition → MCP schema)
│   ├── protocol.py            #   Protocol adapter (error / progress normalization)
│   ├── stdio_guard.py         #   Stdio safety guard (sensitive field filtering, length limits)
│   ├── audit.py               #   Tool call audit logging
│   ├── launchers/             #   MCP launchers (OpenCode MCP config generation)
│   ├── tools/tasks/           #   Task MCP tools (8 tools)
│   │   ├── create_task.py     #     Create task (template validation + task_mutation_guard)
│   │   ├── list_tasks.py      #     List tasks (pagination / filter / sort)
│   │   ├── show_task.py       #     Show task details
│   │   ├── edit_task.py       #     Edit task fields
│   │   ├── stop_task.py       #     Stop running task
│   │   ├── archive_task.py    #     Archive completed task
│   │   ├── validate_task_template.py # Validate task template
│   │   └── generate_breakdown.py     # Generate task breakdown suggestions
│   ├── tools/context/         #   Context MCP tools (6 tools)
│   │   ├── explore.py         #     Project exploration (grep / glob read-only)
│   │   ├── inspect_project.py #     Trigger project inspection
│   │   ├── wiki_query.py      #     Wiki knowledge base query
│   │   ├── wiki_add.py        #     Wiki entry addition
│   │   ├── note_add.py        #     Project note taking
│   │   ├── hook_trigger.py    #     Hook triggering
│   │   └── workflow.py        #     Workflow state read / write
│   ├── tools/ops/             #   Operations MCP tools (5 tools)
│   │   ├── exec.py            #     Controlled command execution
│   │   ├── doctor.py          #     Health diagnostics
│   │   ├── daemon_status.py   #     Daemon status
│   │   ├── build_fix.py       #     Build fix
│   │   └── run_once.py        #     Single task execution
│   └── tools/external/        #   External integration MCP tools (2 tools)
│       ├── feishu_notify.py   #     Feishu message push
│       └── webhook_invoke.py  #     Webhook callback
├── webapp/                    # Web UI backend (HTTP + SSE)
│   ├── server.py              #   ThreadingHTTPServer main + SSE push + static asset serving
│   ├── actions.py             #   Action hub (re-exports)
│   ├── action_requirements.py #   Requirement submission / dispatch (goal → intent → dispatch)
│   ├── action_sessions.py     #   Session management (create / message / stop / permission)
│   ├── action_session_history.py # Session history
│   ├── action_session_records.py  # Session records
│   ├── action_task_ops.py     #   Batch task operations (create / delete / archive / cancel)
│   ├── action_state.py        #   Shared UI state (jobs / events in-memory management)
│   ├── action_workflow.py     #   Workflow actions (artifact next_actions)
│   ├── payloads.py            #   Page payload construction (dashboard / goal / sessions)
│   ├── task_payloads.py       #   Task payload construction (list / detail)
│   ├── live_output_payloads.py #  Live output payloads
│   ├── display_sort.py        #   Task display sort rules
│   ├── schema.py              #   Request / response JSON schemas
│   └── webhook.py             #   Webhook HTTP endpoints
├── web/                       # Web UI frontend (Single Page Application)
│   ├── index.html             #   Main page skeleton
│   ├── app.js                 #   Application entry and routing
│   ├── styles.css             #   Global styles
│   ├── utils.js               #   Utility functions (API calls / formatting)
│   ├── components/            #   UI components (ChatView, Composer, GoalInput)
│   └── boundaries/            #   Boundary components (Session, State, Submission)
├── feishu_bot/                # Feishu / Lark bot
│   ├── command_handlers.py    #   Command dispatch (text message → command routing)
│   ├── card_builders.py       #   Card builders (30+ card templates for tasks / projects / sessions)
│   ├── session_runtime.py     #   OpenCode session binding
│   ├── helpers.py             #   Helper functions (project resolution / pending confirm / dedup)
│   └── constants.py           #   Constants
├── feishu_cards.py            # Feishu card primitives (section / field / note / button)
├── feishu_commands.py         # Feishu command parsing (task ID / status / args)
├── feishu_config.py           # Feishu bot config (App ID / Secret / Node path)
├── feishu_interactions.py     # Feishu interaction callback parsing
├── feishu_runtime.py          # Feishu Node.js runtime management (sidecar start / stop)
├── feishu_worker.mjs          # Feishu long-connection worker (WebSocket → event loop)
├── feishu_notify.mjs          # Feishu notification push (HTTP API calls)
├── opencode/                  # OpenCode TUI integration
│   ├── config.py              #   OpenCodeConfig data model
│   ├── env.py                 #   Environment variable bridging (CODEPILOT_* → OPENCODE_*)
│   ├── paths.py               #   Runtime paths (~/.codepilot/opencode/<project>/)
│   ├── profile.py             #   TUI profile generation (opencode.json + tui.json + config/*)
│   ├── session.py             #   Session isolation (per-project session ID)
│   └── model_state.py         #   User model selection persistence
├── scheduled/                 # Scheduled / Event agents
│   ├── runner.py              #   Agent job execution (CLI subprocess + output parsing)
│   ├── daemon.py              #   Scheduling daemon (interval + cron expressions)
│   ├── guards.py              #   Cost guards (per-job / daily USD cap) + circuit breakers
│   ├── audit.py               #   Audit logging (.codepilot/scheduled/audit.jsonl)
│   ├── triggers.py            #   Event triggers (task.failed / task.done)
│   └── templates.py           #   Built-in agent templates (task_health / daily_summary / auto_inspect)
├── binary_support/            # Binary build and release
│   ├── manager.py             #   PyInstaller build (spec generation / packaging / verification)
│   ├── release.py             #   Release workflow (version / changelog / artifact upload)
│   ├── vendor_fetcher.py      #   Third-party CLI fetch (GitHub Release → cache → bundled)
│   ├── paths.py               #   Path resolution (install dir / resource dir)
│   └── version.py             #   Version management
├── codex/                     # Codex CLI session persistence (JSON file storage)
├── claude/                    # Claude Code session persistence (JSON file storage)
├── mcp/launchers/             # MCP launchers (OpenCode MCP config)
├── templates/                 # Task template markdown (builder / reviewer / repl default prompts)
├── prompts/                   # Prompt templates (planner / classifier system prompts)
└── nl_command_router.py       # Natural-language command router (Chinese / English keyword → structured command)
```

### Data Flow Overview

```
┌─────────────────────────────────────────────────────┐
│              Multi-Channel Input                     │
│  CLI (codepilot go)  Web UI  Feishu  Webhook       │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  nl_command_router / intent_classifier              │
│  Intent recognition → requirement / question         │
│                     / command / inspection           │
└──────────────────────┬──────────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        ▼              ▼              ▼
       plan           auto           go
   (spec→tasks)   (end-to-end)  (natural lang)
        │              │              │
        └──────────────┼──────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  run_orchestrator  (Task Queue Orchestration)        │
│  ├─ Project context resolution + config loading      │
│  ├─ Task workspace prep (direct / branch / worktree) │
│  ├─ Executor selection (builtin / dispatch / auto)   │
│  └─ Result finalization (commit / merge / cleanup)   │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  run_builtin_executor  (Builtin Dual-Phase Engine)   │
│  ┌─────────────────────────────────────────────┐    │
│  │  Phase 1: Builder                           │    │
│  │  codex/claude/opencode exec <prompt>         │    │
│  │  → Generate/modify code, run tests          │    │
│  └──────────────┬──────────────────────────────┘    │
│                 │                                    │
│  ┌──────────────▼──────────────────────────────┐    │
│  │  Phase 2: Reviewer                          │    │
│  │  codex review --uncommitted                  │    │
│  │  → Review changes, output PASS/FAIL verdict  │    │
│  └──────────────┬──────────────────────────────┘    │
│                 │                                    │
│     ┌───────────┴───────────┐                       │
│     ▼                       ▼                       │
│   PASS                   FAIL                       │
│   → done                 → max_rounds reached?      │
│                            │ yes      │ no           │
│                            ▼          ▼              │
│                          failed    backlog           │
│                          (triage)  (builder retry)   │
└─────────────────────────────────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
       codex        claude       opencode
         │             │             │
         └─────────────┼─────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  gateway (AI API Gateway)                            │
│  resolution → execute → format                      │
│  ├─ CLI mode: subprocess invoking CLI agent          │
│  └─ API mode: HTTP POST → OpenAI/Anthropic/DeepSeek │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  storage (SQLite ~/.codepilot/data.db)              │
│  tasks │ sessions │ projects │ service_states        │
│  memory events (.codepilot/memory/)                  │
│  scheduled audit/guards (.codepilot/scheduled/)      │
└─────────────────────────────────────────────────────┘
```

### Key Design Decisions

- **Dual-Phase Agent Loop** (`run_builtin_executor.py`): Every task goes through builder (implements) then reviewer (inspects). The reviewer outputs a structured PASS/FAIL verdict; on FAIL the builder receives reviewer feedback and retries, up to `max_review_rounds`. Exceeding the limit triggers the deterministic failure triage pipeline.
- **Failure Triage Pipeline** (`run_failure_triage*.py`): On task failure, the system collects evidence (exit code, stderr, agent output, task context), a classifier determines the action (retry_with_hint / replan / discard / merge_partial), and the repair is auto-executed after LLM prompt construction.
- **Workflow State Machine** (`core/workflow_state.py`): Project-level mode state manages plan → execute phase transitions. Each phase produces structured artifacts (plan, context) persisted as atomic JSON files under `.codepilot/state/`, enabling pause-and-resume.
- **Memory System** (`core/memory.py`): Append-only factual event log → deduplicated candidate generation → feedback scoring (positive/negative/neutral) → merged seen_count → auto-captured as `autocapture.md`. Event sources cover task state changes, workflow action execution, inspection report feedback, and more.
- **MCP Tool Three-Layer Architecture**: Task tools (protected by `task_mutation_guard` state machine) → Context tools (read-only, no file mutation) → External integration tools (bridge Feishu/Webhook). Each layer has independent audit and security policies.
- **Agent Family Fallback Chain**: `fallback_cli_order` defines CLI agent priority. When one agent is unavailable (not installed / no key / timeout), the system automatically switches to the next. During dual-phase execution, if the builder's agent fails, the system attempts to swap builder/reviewer agents or fall back to another available family.
- **Three-Layer Configuration Overlay**: `~/.codepilot/AGENTS.toml` (global defaults) → project `AGENTS.toml` (per-project overrides) → `.codepilot.secrets.toml` (API keys and other sensitive values, never committed to git). Loading includes schema validation with intelligent error messages.
- **Task Workspace Isolation**: Three modes — `direct` (execute in main working directory), `branch` (git branch isolation), `worktree` (independent git worktree, symlinks node_modules and other dependency directories). The dirty-worktree preflight policy is configurable as stop / commit / stash.
- **OpenCode Brand Isolation**: On launch, generates a complete runtime configuration under `~/.codepilot/opencode/<project>/` (MCP server, tools, permissions, TUI plugin, instructions), injecting CodePilot's brand, agent definitions, and permission policy into the official OpenCode binary without modifying its source.

### Test Suite

The project follows TDD methodology with ~150 test files covering all core modules:

| Layer | Description | Run Command |
| --- | --- | --- |
| **Unit Tests** | Per-module isolated tests with mocked external dependencies | `pytest tests/ -m "not slow"` (daily ~60-90s) |
| **Integration Tests** | Cross-module boundary tests (DB + CLI + MCP + Web UI API) | `pytest tests/` full suite (~3.5min with xdist) |
| **Slow Tests** | Git worktree, full pipeline, self-iteration cases >10s each | `pytest tests/ -m "slow"` (tagged `slow`) |
| **Serial Tests** | Cases holding global locks, ports, or daemons | Tagged `serial`, run on same xdist worker |
| **E2E** | Web UI asset integrity, binary build/install/release flow | `pytest tests/test_web_assets.py tests/test_workflow_binary_release.py` |

Test infrastructure: `conftest.py` provides shared fixtures (temporary project / database / config); `*_testkit.py` files provide reusable test utilities (AI gateway mock, Feishu bot mock, chat flow builder, MCP stdio shim).

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

- **Quickstart: [中文](docs/01-快速上手指南.zh-CN.md) / [English](docs/01-quickstart-guide.en-US.md)**
- Overview: [中文](docs/02-说明文档.zh-CN.md) / [English](docs/02-overview.en-US.md)
- Operation guide: [中文](docs/03-操作文档.zh-CN.md) / [English](docs/03-operation-guide.en-US.md)
- AI / Agent guide: [中文](docs/04-AI与Agent调用手册.zh-CN.md) / [English](docs/04-ai-agent-manual.en-US.md)
- Skill integration guide: [中文](docs/05-Skill化集成指南.zh-CN.md) / [English](docs/05-skill-integration-guide.en-US.md)
- Project services: [中文](docs/06-项目服务改造说明.zh-CN.md) / [English](docs/06-project-services.en-US.md)
- Workflow state conventions: [中文](docs/07-workflow-state.zh-CN.md) / [English](docs/07-workflow-state.en-US.md)
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

The Skill itself is written in English for other Codex / Agent runtimes. Installation and maintenance notes are in the Skill integration guide: [中文](docs/05-Skill化集成指南.zh-CN.md) / [English](docs/05-skill-integration-guide.en-US.md).

## License

This project is open sourced under the [MIT License](LICENSE).
