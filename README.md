# CodePilot

CodePilot is a local workflow CLI for turning one engineering goal into queued implementation tasks and running them automatically.

## Current capabilities

- Register projects with `AGENTS.toml`
- Queue tasks in SQLite
- Accept plain text requirements directly with `codepilot "你的需求"`
- Allow choosing the task agent when planning, such as `--agent codex` or `--agent claude`
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

## Build a native binary

Build a single-file binary for the current OS:

```bash
pip install .[build]
codepilot binary build
```

Build and install it into a user-local command directory:

```bash
codepilot binary build --install
```

Install an existing binary and register `codepilot` on the user PATH:

```bash
codepilot binary install --binary ./dist/binary/linux-x86_64/codepilot
codepilot binary install --binary .\\dist\\binary\\windows-x86_64\\codepilot.exe
```

Show the default install directory:

```bash
codepilot binary where
```

Notes:

- Windows and Linux binaries must be built natively on each OS. This command does not cross-compile.
- `binary install` writes into a user-local directory and updates the user PATH when needed.
- On Windows the default target is `%LOCALAPPDATA%\\Programs\\CodePilot\\bin`.
- On Linux the default target is `~/.local/bin`.

## Notes

- The default project config now uses `default_mode = "codex"` and `planner = "codex"` in `AGENTS.toml`.
- `AGENTS.toml` under `[agents]` now really overrides CLI paths such as `codex_cmd` and `claude_cmd`, so non-standard installs can be used directly by planning and builtin execution.
- The built-in executor currently supports `codex`, `claude`, `claude-node`, `claude-sonnet`, `claude-opus`, `claude-haiku`, and `dual`.
- The planner currently supports `claude` and `codex` for structured task decomposition.
- If Codex planning times out, CodePilot will fall back to a single task and continue execution instead of stopping at the planning stage.
- When builtin execution runs with `auto_commit = true`, CodePilot now preflights Git state and will skip execution early if the project is not yet a Git repo or already has uncommitted changes.
- Use `codepilot --no-execute "你的需求"` to plan only.
- In chat mode, use `/help`, `/status`, `/project <name>`, `/agent <name>`, `/execute on|off`, `/exit`.
