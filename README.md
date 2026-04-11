# CodePilot

CodePilot is a local workflow CLI for turning one engineering goal into queued implementation tasks and running them automatically.

## Current capabilities

- Register projects with `AGENTS.toml`
- Queue tasks in SQLite
- Accept plain text requirements directly with `codepilot "你的需求"`
- Start a persistent interactive session with `codepilot chat` or simply `codepilot`
- Decide whether a requirement is simple or complex, then either keep it as one task or split it into sequential subtasks
- Run queued tasks with either:
  - an external dispatch script, or
  - the built-in `codex exec` executor
- Review uncommitted changes with Codex review and optionally auto-commit each completed task

## Quick start

```bash
codepilot init .
codepilot "实现自动拆分和自动执行工作流"
codepilot chat
```

## Notes

- The built-in executor currently targets Codex CLI for execution.
- The planner currently uses Claude CLI for structured task decomposition.
- Use `codepilot --no-execute "你的需求"` to plan only.
- In chat mode, use `/help`, `/status`, `/project <name>`, `/execute on|off`, `/exit`.
