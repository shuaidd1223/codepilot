# Claude Code Instructions

本文件补充 `AGENTS.md`，用于 Claude Code 在本仓库中执行任务时的行为约束。若两者冲突，以 `AGENTS.md` 为准。

## Operating Mode

- 使用非交互方式完成任务，不等待人工逐步确认。
- 先读任务文件和相关项目规范，再读取必要代码；不要一次性展开无关目录。
- 只修改任务范围内的文件。若必须偏离范围，在最终说明里解释原因和风险。
- 不要运行破坏性 git 命令，不要回滚用户已有改动。

## TDD Workflow

1. Red：先补或调整一个能暴露目标行为的测试，并确认它在旧实现下会失败；如果无法实际运行失败态，说明原因。
2. Green：用最小实现让测试通过，不顺手重构无关代码。
3. Refactor：只在有明确收益且不扩大任务范围时整理代码。
4. Verify：运行相关测试和必要的回归检查，最终报告命令与结果。

## CodePilot Project Notes

- 自然语言需求优先走 `codepilot "需求文本"` 或 `codepilot go "需求文本"`。
- 任务运维统一用 `codepilot task ...`；发布统一用 `codepilot binary ...`。
- `add -f` 面向外部智能体批量投递，必须提供符合 `codepilot ai template --format json` 的完整任务内容。
- `chat`、Web UI 和飞书自由文本创建工作时必须使用 `# <需求>` 或 `! <任务>`。

## Review Discipline

- Reviewer 只按任务验收标准、范围和验证结果判断，不扩展到无关技术债。
- Builder 必须把 TDD 证据写进交付说明：新增/修改的测试、验证命令、未覆盖项。
