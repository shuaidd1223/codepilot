# CodePilot AI Usage Guide

Language: [简体中文](AI_USAGE.zh-CN.md) | English

**Author:** 帅呆呆 <2264505396@qq.com> | **Repository:** https://gitee.com/shuai_dd/CodePilot | **License:** MIT

This guide is for other AI agents. For the latest machine-readable command list, use `codepilot ai manifest`. For this Markdown guide, use `codepilot ai guide`.

## Core Calling Rules

1. Prefer non-interactive commands.
2. Prefer `--json` or `codepilot ai manifest` when structured output is needed.
3. Submit high-level requirements with `codepilot "requirement text"` or `codepilot go "requirement text"`.
4. Treat questions about project status, task counts, completion, failed tasks, running tasks, or service status as Q&A.
5. `chat`, Web UI sessions, and Feishu free text enter OpenCode + CodePilot MCP and can receive questions, requirements, or operation intent directly.
6. Use `codepilot task ...` for task operations.
7. Use `codepilot binary ...` for releases.
8. External AI systems must read `codepilot ai template --format json` before submitting tasks directly.
9. Use `clarify` / `plan` explicitly when a deterministic spec or plan artifact is needed.

## Recommended Commands

### Project Setup

```bash
codepilot setup . --dry-run --json
codepilot setup .
codepilot doctor --project <project-name> --services --json
```

### Submit Requirements

```bash
codepilot "fix task retry logic and add tests"
codepilot go "fix task retry logic and add tests" -p <project-name>
```

### Clarify, Plan, and Safe Next Actions

```bash
codepilot clarify -p <project-name> "vague requirement" --json
codepilot plan -p <project-name> "clear requirement" --json
codepilot inspect -p <project-name> --once --dry-run --write-workflow --json
codepilot workflow status -p <project-name> --json
codepilot workflow next -p <project-name> --list --json
codepilot workflow next -p <project-name> --action <id> --json
codepilot workflow next -p <project-name> --auto --json
```

`clarify`, `plan`, and `inspect --write-workflow` create reviewable artifacts and record `next_actions`, but they do not create backlog tasks or start execution by themselves. Use `workflow next --list` to inspect available actions, `workflow next --action <id>` to execute an allowlisted action, or `workflow next --auto` to let CodePilot choose one low-risk policy action. `suggested_command` is only display/review metadata; do not compose or execute it automatically. High-risk actions still require explicit confirmation and must pass the workflow next allowlist.

### Status and Evidence

```bash
codepilot status -p <project-name> --json
codepilot hud -p <project-name> --preset full --json
codepilot explore -p <project-name> --prompt "find task template" --json
codepilot trace -p <project-name> --limit 30 --json
codepilot memory events -p <project-name> --json
```

`memory events` reads automatic factual observations from `.codepilot/memory/events.jsonl`. CodePilot turns them into deduplicated candidates and maintains `.codepilot/memory/autocapture.md`; candidates record `score`, `feedback`, and `seen_count`, with workflow actions and terminal task outcomes automatically adjusting weight. It does not directly write human-maintained long-term wiki/note content.

### Task Template

If an external AI submits tasks via `add -f tasks.json` / `add -f tasks.md`, it must read the template first:

```bash
codepilot ai template
codepilot ai template --format json
codepilot ai template --format guide
```

Required rules:

1. Humans should use `codepilot "requirement text"` instead of direct `add`.
2. Each `tasks.json` item must include template-compliant `content`.
3. Each `tasks.md` section must be a complete task template.
4. `add -t "title"` and `tasks.txt` call AI to generate content and validate it.
5. `--no-ai` / `--allow-empty` are removed; empty placeholder tasks are not allowed.
