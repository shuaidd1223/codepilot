# Task #96 输入一致性核对

## 核对结论
- 用户需求与侦察报告均聚焦同一目标：补齐规划输入（用户需求 + 侦察结论）的可复核交付。
- 侦察报告引用的代码位置均位于当前仓库 `dev` 分支，且与“规划输入注入”主题直接相关。
- 未引入需求外目标（如重构 run 全链路、扩展新执行器能力、调整无关模块）。

## 对照表
- 需求点：规划器需要“用户需求原文”输入。
  - 证据：`codepilot/ai.py:1153`，`codepilot/prompts/task_breakdown.md:62`，`codepilot/prompts/task_breakdown.md:63`。
- 需求点：规划器需要“侦察报告”输入。
  - 证据：`codepilot/ai.py:1155`，`codepilot/prompts/task_breakdown.md:65`，`codepilot/prompts/task_breakdown.md:66`，`codepilot/ai.py:844`。
- 需求点：两阶段流程下侦察结果会进入规划提示。
  - 证据：`codepilot/commands/auto_workflow.py:422`，`codepilot/commands/auto_workflow.py:434`，`tests/test_planner_enhancements.py:391`。

## 进入拆分前置条件
- `user-requirement.md` 与 `recon-report.md` 均存在且内容完整。
- `recon-report.md` 的 `file_path:line` 可在当前分支检索命中。
- 若以上任一条件不满足，本任务不进入下一步拆分。
