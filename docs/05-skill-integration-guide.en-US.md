# CodePilot Skill Integration Guide

Language: [中文](05-Skill化集成指南.zh-CN.md) | English

This guide explains how to expose CodePilot as a reusable Skill for other AI agents while keeping the Skill aligned with the current CLI command structure.

## 1. Skill Directory

Repository Skill package:

```text
skills/codepilot-workflow/
```

Important files:

- `SKILL.md`: trigger rules, hard constraints, and the main workflow.
- `references/command-map.md`: command lookup and common invocation patterns.
- `references/agent-playbooks.md`: recommended workflows for requirement intake, status reads, repair loops, and release work.

Convention: the Skill package itself is English because it is consumed by agent runtimes. Human-facing repository docs are bilingual in `docs/`.

## 2. Installation

### 2.1 Copy To A Local Skill Directory

Copy `skills/codepilot-workflow/` into the target agent's skill directory. Keep the directory name stable so existing trigger phrases continue to work.

### 2.2 Install From A Git Subdirectory

If the target agent supports Git-based skill installation, point it at the repository and the `skills/codepilot-workflow` subdirectory.

### 2.3 Sparse Checkout

For environments that should not fetch the whole repository, sparse-checkout only the Skill directory:

```bash
git sparse-checkout set skills/codepilot-workflow
```

## 3. Trigger Scenarios

Use this Skill when an AI agent needs to:

- Inspect CodePilot project/task/service status.
- Submit a natural-language requirement to CodePilot.
- Clarify or plan a requirement before execution.
- Retry, stop, resume, or inspect tasks.
- Debug failed tasks through `build-fix`.
- Use Web UI, Feishu, webhook, event, hook, provider, or skill integration commands.
- Prepare binary release artifacts.

## 4. Recommended Calling Chains

### 4.1 New Requirement

```bash
codepilot "requirement text"
codepilot status -p <project-name> -v
codepilot task show <task_id>
codepilot task logs <task_id> --tail 80
```

### 4.2 Clarify Then Plan

```bash
codepilot clarify -p <project-name> "vague requirement" --json
codepilot plan -p <project-name> --from-spec .codepilot/specs/<file>.md --json
```

### 4.3 Project Q&A

```bash
codepilot go "how many tasks are failed?" -p <project-name>
codepilot explore -p <project-name> --prompt "recent failed task logs" --json
```

### 4.4 Interactive Channels

```bash
codepilot chat -p <project-name> -a opencode
codepilot feishu start
codepilot ui start
```

## 5. Minimal Cross-Agent Constraints

- Prefer non-interactive commands unless an ongoing session is required.
- Prefer `--json` for automation.
- Do not submit empty or placeholder tasks.
- Read `codepilot ai template --format json` before using `add -f`.
- Use `codepilot task ...` for task operations and `codepilot binary ...` for release operations.
- Keep secrets out of docs, task content, wiki notes, and Skill references.

## 6. Maintenance Checklist

When CodePilot command behavior changes, review these files together:

- [README.md](../README.md) / [README.en-US.md](../README.en-US.md)
- [docs/说明文档.zh-CN.md](02-说明文档.zh-CN.md) / [docs/说明文档.en-US.md](02-overview.en-US.md)
- [docs/操作文档.zh-CN.md](03-操作文档.zh-CN.md) / [docs/操作文档.en-US.md](03-operation-guide.en-US.md)
- [docs/AI与Agent调用手册.zh-CN.md](04-AI与Agent调用手册.zh-CN.md) / [docs/AI与Agent调用手册.en-US.md](04-ai-agent-manual.en-US.md)
- [docs/Skill化集成指南.zh-CN.md](05-Skill化集成指南.zh-CN.md) / [docs/Skill化集成指南.en-US.md](05-skill-integration-guide.en-US.md)
- [AI_USAGE.zh-CN.md](../AI_USAGE.zh-CN.md) / [AI_USAGE.en-US.md](../AI_USAGE.en-US.md)
- `skills/codepilot-workflow/SKILL.md`
- `skills/codepilot-workflow/references/command-map.md`
- `skills/codepilot-workflow/references/agent-playbooks.md`

## 7. Validation Suggestions

For documentation-only and Skill-only changes, smoke-check the generated AI-facing outputs and key docs:

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai guide --language zh-CN
codepilot ai template --format json
codepilot ai template --format guide
```

If only documentation and Skill content changed, full pytest is not required. The delivery note should state that no runtime behavior changed and list the manual checks above.
