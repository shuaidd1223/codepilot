# Task #96 侦察报告（结构化）

## current_state
- 当前自动规划链路已经支持两阶段流程：先侦察、再拆分。
- 规划阶段会把用户需求（`title`）与侦察结论（`recon_block`）注入 `task_breakdown` 提示词。
- 上轮阻塞不在“能力缺失”，而在“输入未落盘”，导致无法被 reviewer 和后续流程复核。

## relevant_files
- `codepilot/commands/auto_workflow.py:364` `run_requirement_workflow` 是自然语言需求进入规划流程的入口。
- `codepilot/commands/auto_workflow.py:422` 两阶段开关开启时，明确走“侦察项目 -> 拆分任务”路径。
- `codepilot/commands/auto_workflow.py:434` 调用 `generate_task_breakdown(..., two_stage=two_stage_enabled)`。
- `codepilot/ai.py:844` `_run_recon_stage` 负责产出侦察结果（`RECON_SCHEMA`）。
- `codepilot/ai.py:1110` `generate_task_breakdown` 负责组装规划输入并调用 planner。
- `codepilot/ai.py:1152` `TASK_BREAKDOWN_PROMPT_TEMPLATE.format(...)` 注入规划字段。
- `codepilot/ai.py:1153` 注入用户需求 `title=title`。
- `codepilot/ai.py:1155` 注入侦察结论 `recon_block=recon_block`。
- `codepilot/prompts/task_breakdown.md:62` 模板声明“=== 用户需求 ===”段。
- `codepilot/prompts/task_breakdown.md:63` 模板消费 `{title}`。
- `codepilot/prompts/task_breakdown.md:65` 模板声明“=== 侦察员结论 ===”段。
- `codepilot/prompts/task_breakdown.md:66` 模板消费 `{recon_block}`。
- `tests/test_planner_enhancements.py:391` 回归用例验证两阶段时侦察内容进入规划提示。

## key_findings
- 规划提示词模板与调用代码都已具备“用户需求 + 侦察结论”输入位点。
- 风险点在执行流程层面：如果输入只停留在终端输出而不落盘，reviewer 无法进行“可追溯复核”。
- 当前仓库此前没有任务 #96 的输入工件，本次需补齐可持久化文件。

## risks
- 若后续代码移动导致行号变化，`file_path:line` 需随任务更新。
- 若跳过两阶段规划（`two_stage=False`），侦察输入不会生成；需在任务级说明这是预期行为。
- 若只更新需求文件而不更新侦察文件，范围一致性会失效，导致规划偏移。

## dependencies
- 依赖 `codepilot/ai.py` 中 `_run_recon_stage` 与 `generate_task_breakdown` 的当前行为。
- 依赖 `codepilot/prompts/task_breakdown.md` 模板占位符不被移除。
- 依赖 `tests/test_planner_enhancements.py` 对输入注入行为的回归保护。

## suggested_approach
- 将本任务规划输入落盘在 `docs/planner-inputs/task-96/`：
  - `user-requirement.md`：记录用户需求原文、动机、成功标准。
  - `recon-report.md`：记录结构化侦察结论与 `file_path:line` 证据。
  - `consistency-check.md`：核对两份输入覆盖同一需求范围后再进入任务拆分。
