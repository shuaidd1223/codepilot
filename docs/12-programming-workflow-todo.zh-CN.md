# CodePilot 编程工作流优化 TODO

维护日期：2026-05-25

本文用于固化“参考 Cline、Open SWE、Plandex、OpenHands、OpenCode、Aider 后，继续优化 CodePilot 编程工作流”的任务规划。后续会话优先读取本文，再决定是否拆分为 `codepilot plan` 或 `codepilot add -f` 的具体任务。

## 当前定位

CodePilot 不应演进成单一 coding agent。更适合继续保持“本地编程工作流编排层”的定位：

- 把自然语言需求转成可审查计划和可执行任务。
- 把 OpenCode、Codex、Claude、Aider 等执行器组织成统一任务流。
- 对执行过程提供任务状态、日志、产物、审查、重试、回滚和通知能力。
- 通过 CLI、Web UI、飞书、Webhook、MCP 暴露一致的编程工作流入口。

## 参考项目与可借鉴点

- Cline：任务板、多 agent、Plan/Act、定时任务、MCP、IDE/CLI/SDK 多入口。
- Open SWE：异步任务入口、GitHub/Slack/Linear 触发、沙箱执行、自动 PR。
- Plandex：长期计划、上下文管理、累计 diff sandbox、人工 review 后应用。
- OpenHands：平台化 agent runtime、SDK/CLI/GUI 分层、执行环境隔离。
- OpenCode：多会话、多模型 provider、TUI 交互、MCP 配置和工具扩展。
- Aider：git-native 代码编辑、测试、提交的内循环体验。

## 设计原则

- 强化工作流编排，不复制执行器本身。
- 先让 CLI 和 JSON 输出闭环，再补 Web UI、飞书和 MCP。
- 所有自动动作都必须留下 artifact、日志和可追踪时间线。
- 高风险动作必须可审查、可拒绝、可重试、可回滚。
- 主控 agent 只能基于结构化事实做分析和导向，不能凭空读取未记录的 agent 对话。
- 兼容当前 `task_workspace = "direct"` 和未来 worktree/sandbox 模式。
- 任何代码改动遵守仓库 TDD 规则：先补失败测试，再做最小实现。

## 非目标

- 不把核心能力绑定到某一个 agent 或某一家模型。
- 不把 Web UI 做成纯聊天界面。
- 不在第一阶段引入云端沙箱、远程执行平台或 GitHub App。
- 不为了并行 agent 大改现有任务状态机。
- 不默认自动提交、自动推送或自动开 PR。
- 不让主控 agent 直接执行任意 shell 字符串；所有控制动作必须走 allowlist。

## 阶段一：Web UI 任务板

目标：把现有 Web UI 从任务列表和会话面板升级成编程工作流任务板，让用户能直接看到需求、任务、执行、审查、阻塞和完成状态。

建议状态列：

- `Backlog`：已创建但未准备执行。
- `Ready`：验收标准和验证命令齐全，可执行。
- `Running`：被 daemon 或手动 run claim。
- `Review`：执行完成但需要查看 diff、日志或 reviewer verdict。
- `Blocked`：缺配置、缺上下文、测试失败或等待人工决策。
- `Done`：已完成并通过验证。

涉及路径：

- `codepilot/web/index.html`
- `codepilot/web/styles.css`
- `codepilot/web/components/ChatView.js`
- `codepilot/web/boundaries/AppStateBoundary.js`
- `codepilot/web/boundaries/TaskDetailBoundary.js`
- `codepilot/webapp/server.py`
- `codepilot/webapp/action_state.py`
- `codepilot/webapp/action_task_ops.py`
- `codepilot/webapp/task_payloads.py`
- `tests/test_webui_api.py`
- `tests/test_web_assets.py`

第一批任务：

1. 为 Web UI API 增加任务板 payload，按状态列返回任务、统计和排序信息。
2. 在前端增加任务板视图，保留现有任务详情和日志入口。
3. 增加阻塞原因展示，至少覆盖配置错误、执行失败、测试失败、等待审查。
4. 增加从任务板触发 `retry`、`cancel`、`archive`、`show logs` 的入口。

验收标准：

- Web UI 能同时展示 6 个任务阶段及每列数量。
- 点击任务后能打开原有详情，不丢失日志、状态和操作按钮。
- API 返回 JSON 结构稳定，并覆盖空任务、失败任务、运行中任务。
- 前端静态资源测试通过。

验证命令：

```powershell
pytest -m "not slow" tests/test_webui_api.py tests/test_web_assets.py
```

风险边界：

- 不修改任务数据库 schema，优先用现有状态映射视图列。
- 不改 daemon claim 语义。
- 不引入拖拽排序作为第一批能力。

## 阶段二：Diff Sandbox 与变更审查区

目标：参考 Plandex，把 agent 产出的代码改动沉淀成可审查 artifact。用户在接受、重试或丢弃前，能看到 diff、测试结果和 reviewer verdict。

核心概念：

- `patch artifact`：一次任务执行产生的候选变更。
- `validation artifact`：测试命令、退出码、摘要和关键日志。
- `review artifact`：reviewer 判定、风险点、是否建议接受。
- `apply decision`：人工或策略选择 `apply`、`retry`、`discard`、`replan`。

涉及路径：

- `codepilot/commands/run.py`
- `codepilot/commands/run_builtin_core.py`
- `codepilot/commands/run_failure_triage_apply.py`
- `codepilot/commands/build_fix.py`
- `codepilot/core/workflow_state.py`
- `codepilot/webapp/action_workflow.py`
- `codepilot/webapp/task_payloads.py`
- `tests/test_run_live_runner_simplified.py`
- `tests/test_build_fix_command.py`
- `tests/test_workflow_state.py` 或新增 `tests/test_patch_artifacts.py`

第一批任务：

1. 在任务执行结束时记录 diff 摘要和验证摘要，不改变现有应用代码的时机。
2. 为每个任务暴露 `artifacts.patch`、`artifacts.validation`、`artifacts.review` 的只读查询。
3. 在 Web UI 任务详情中展示 patch 摘要、测试结果和 reviewer verdict。
4. 为失败任务增加 `retry with reviewer feedback` 的结构化入口。

验收标准：

- 成功任务和失败任务都能查询到执行 artifact 元数据。
- 没有 git diff 时返回明确的 empty artifact，不报错。
- 失败任务能保留失败命令、退出码和关键 stderr 摘要。
- Web UI 不直接执行任意 shell 字符串。

验证命令：

```powershell
pytest -m "not slow" tests/test_run_live_runner_simplified.py tests/test_build_fix_command.py tests/test_webui_api.py
```

风险边界：

- 第一批只做记录和展示，不做真正的 patch apply/discard 机制。
- `direct` 工作区模式下必须避免误删用户已有未提交改动。
- 不自动提交、不自动 reset、不自动 checkout。

## 阶段三：统一 Intake 与 Work Item

目标：参考 Open SWE，把 CLI、Web UI、飞书、Webhook、MCP、任务文件输入统一成 `work_item`，保留来源和回调通道。

涉及路径：

- `codepilot/commands/auto.py`
- `codepilot/commands/add.py`
- `codepilot/commands/requirement_worker.py`
- `codepilot/webapp/action_requirements.py`
- `codepilot/webapp/webhook.py`
- `codepilot/feishu_bot/`
- `codepilot/mcp/tools/tasks/create_task.py`
- `codepilot/ai_support/main_resolution.py`
- `tests/test_auto_workflow_task_spec.py`
- `tests/test_webui_api.py`
- `tests/test_feishu_bot_project_events.py`
- `tests/test_mcp_tools_tasks.py`

第一批任务：

1. 定义内部 `work_item` 数据结构，包含 source、project、requester、context_links、callback、raw_text。
2. 将 CLI requirement、Web UI requirement 和 webhook payload 先适配到该结构。
3. 为飞书和 MCP 创建轻量适配层，不改变现有命令行为。
4. 在任务详情中展示来源信息和原始需求摘要。

验收标准：

- 不同入口创建的任务能保留来源和上下文链接。
- 旧的 `codepilot go`、`codepilot add -f`、Webhook `/tasks` 行为兼容。
- source 字段缺失时有默认值，不影响旧数据读取。

验证命令：

```powershell
pytest -m "not slow" tests/test_auto_workflow_task_spec.py tests/test_webui_api.py tests/test_mcp_tools_tasks.py
```

风险边界：

- 不在第一批迁移数据库 schema，优先将来源信息放进任务 metadata 或 artifact。
- 不引入 GitHub、Linear、Slack 真实集成，只预留字段。

## 阶段四：Executor Contract

目标：把当前 CLI family 和 fallback 机制升级为稳定 executor contract，让不同执行器只需要实现统一阶段。

建议 contract：

1. `prepare`：检查模型、权限、工作区和上下文。
2. `execute`：执行任务提示词，收集 stdout、stderr、退出码。
3. `observe`：提取 diff、日志、测试结果、文件触达范围。
4. `validate`：运行验证命令或解析执行器自带验证结果。
5. `review`：调用 reviewer 或本地规则产出 verdict。
6. `repair`：基于失败证据生成最小修复任务或重试提示。

涉及路径：

- `codepilot/ai_support/cli_families.py`
- `codepilot/ai_support/family_runtime.py`
- `codepilot/ai_support/main_execute.py`
- `codepilot/ai_support/main_resolution.py`
- `codepilot/gateway/execute.py`
- `codepilot/gateway/resolution.py`
- `codepilot/commands/run_builtin_core.py`
- `tests/test_cli_families_registry.py`
- `tests/test_gateway_resolution_registry.py`
- `tests/test_ai_gateway_stage_execute_dispatch.py`
- `tests/test_workflow_agent_resolution.py`

第一批任务：

1. 为现有 claude、codex、opencode family 写 contract 映射文档和测试。
2. 将 fallback 失败原因标准化为结构化 enum。
3. 在任务日志和 trace 中显示 executor family、model、fallback path。
4. 为未来 aider executor 预留注册点，但不实现真实调用。

验收标准：

- 现有三类 family 行为不变。
- fallback 失败原因能在 JSON 输出和 trace 中查询。
- 执行器不可用、超时、权限不足三类路径都有测试覆盖。

验证命令：

```powershell
pytest -m "not slow" tests/test_cli_families_registry.py tests/test_gateway_resolution_registry.py tests/test_ai_gateway_stage_execute_dispatch.py tests/test_workflow_agent_resolution.py
```

风险边界：

- 不改变默认 `fallback_cli_order`。
- 不把 provider 配置迁移到新格式。
- 不在第一批支持执行器插件热加载。

## 阶段五：任务时间线与可恢复会话

目标：强化 `trace`、`workflow_state`、`memory` 和 Web UI，让每个任务都有可恢复、可解释、可审计的编程工作流时间线。

涉及路径：

- `codepilot/core/workflow_state.py`
- `codepilot/core/memory.py`
- `codepilot/core/progress_bus.py`
- `codepilot/commands/trace.py`
- `codepilot/commands/workflow.py`
- `codepilot/webapp/action_state.py`
- `codepilot/webapp/action_session_history.py`
- `codepilot/ai_support/opencode_runtime.py`
- `tests/test_workflow_state.py`
- `tests/test_wiki.py`
- `tests/test_codex_session_storage.py`
- `tests/test_webui_opencode_session.py`

第一批任务：

1. 定义任务时间线事件：created、planned、claimed、agent_started、diff_detected、validated、reviewed、blocked、done、failed。
2. 将关键事件写入 workflow state 或现有事件日志。
3. Web UI 任务详情增加 timeline 区域。
4. `trace` 支持按 task_id 过滤并输出 JSON。

验收标准：

- 同一个任务能从 CLI 和 Web UI 查询到一致时间线。
- 时间线事件包含时间、actor、source、message、artifact_path。
- 老任务缺 timeline 时能正常降级显示。

验证命令：

```powershell
pytest -m "not slow" tests/test_workflow_state.py tests/test_webui_api.py tests/test_codex_session_storage.py
```

风险边界：

- 不把 timeline 作为任务状态机唯一事实源。
- 不把大段 agent 输出写入 timeline，只存摘要和路径。

## 阶段六：主控 Supervisor Agent

目标：增加一个“主控 agent”监听整个编程任务流程，持续分析当前方向是否正确，并通过受控动作影响后续执行。它不是新的代码执行器，而是工作流观察者、分诊者和方向控制器。

职责边界：

- 监听任务、workflow、progress、scheduled/event、artifact、validation、reviewer verdict。
- 分析任务是否偏离原始需求、是否需要补上下文、是否需要暂停、重试、拆分、合并或转人工。
- 给出下一步控制建议，并在低风险场景下自动执行 allowlist 动作。
- 将分析结论写入 timeline、workflow state 和 Web UI，而不是只写在 agent 临时上下文中。

输入信号：

- 任务状态：backlog、running、failed、blocked、done、archived。
- 执行事件：agent_started、command_started、command_failed、diff_detected、validated、reviewed。
- Artifact：plan、patch、validation、review、inspect report、memory candidate。
- 配置和护栏：成本限制、循环次数、失败次数、自动推进策略、人工确认策略。

可控动作：

- `pause_task`：暂停或阻塞高风险任务。
- `retry_with_hint`：基于失败证据生成重试提示。
- `request_clarification`：缺需求或缺上下文时转人工确认。
- `replan_from_evidence`：用已有 artifact 生成新 plan。
- `split_task`：任务过大或方向发散时建议拆分。
- `mark_blocked`：记录阻塞原因和恢复条件。
- `promote_to_review`：执行完成但需要人工看 diff 时转入 Review。
- `ignore_low_value_signal`：忽略低价值 inspect 或重复事件。

涉及路径：

- `codepilot/core/event_plugins.py`
- `codepilot/core/progress_bus.py`
- `codepilot/core/workflow_state.py`
- `codepilot/commands/workflow.py`
- `codepilot/commands/inspect_workflow.py`
- `codepilot/commands/scheduled.py`
- `codepilot/scheduled/runner.py`
- `codepilot/scheduled/daemon.py`
- `codepilot/scheduled/guards.py`
- `codepilot/webapp/action_workflow.py`
- `codepilot/mcp/tools/context/workflow.py`
- `tests/test_event_plugins.py`
- `tests/test_workflow_state.py`
- `tests/test_workflow_runtime_control_status.py`
- `tests/test_scheduled_agent_job.py`
- `tests/test_scheduled_daemon.py`
- `tests/test_scheduled_guards.py`

第一批任务：

1. 定义 Supervisor 观察事件模型，复用 timeline 事件，不新增第二套事实源。
2. 增加只读分析命令：输出当前任务流风险、偏离点、阻塞点和建议动作。
3. 增加 allowlist 控制动作执行器，只允许 `mark_blocked`、`request_clarification`、`retry_with_hint` 这类低风险动作。
4. 将 Supervisor 结论展示到 Web UI 任务详情和 workflow 面板。
5. 为 scheduled/event agent 增加 supervisor dry-run 模式，用于周期性巡检任务流。

验收标准：

- Supervisor dry-run 能读取任务、workflow state、timeline 和 artifact 摘要，并输出结构化 JSON。
- 相同输入产生稳定建议，不依赖随机自由文本。
- 自动控制动作必须记录 actor、reason、source_event、artifact_path。
- 高风险动作默认只建议，不执行。
- 循环控制生效：同一任务连续建议相同动作超过阈值时降级为人工确认。

验证命令：

```powershell
pytest -m "not slow" tests/test_event_plugins.py tests/test_workflow_state.py tests/test_workflow_runtime_control_status.py tests/test_scheduled_agent_job.py tests/test_scheduled_guards.py
```

风险边界：

- 不让 Supervisor 绕过任务状态机直接改数据库。
- 不让 Supervisor 直接 shell 执行 `suggested_command`。
- 不把 Supervisor 的自由文本判断当作最终事实；事实仍来自任务状态、artifact 和验证结果。
- 第一批不做跨项目全局调度，只监听单项目任务流。

## 推荐执行顺序

1. 阶段一：Web UI 任务板。
2. 阶段二：Diff Sandbox 与变更审查区。
3. 阶段五：任务时间线与可恢复会话。
4. 阶段六：主控 Supervisor Agent。
5. 阶段三：统一 Intake 与 Work Item。
6. 阶段四：Executor Contract。

原因：

- 阶段一和阶段二最直接提升编程工作流体验。
- 阶段五为后续异步任务和多入口提供可追踪基础。
- 阶段六依赖 timeline 和 artifact 提供事实输入，适合在可观察性稳定后落地。
- 阶段三会影响入口面较广，适合在任务板和 artifact 稳定后做。
- 阶段四偏底层抽象，应该等上层痛点更明确后收敛。

## 下一步建议

下一次开始实现时，优先为“阶段一：Web UI 任务板”创建计划 artifact，要求 planner 输出 3 到 5 个小任务，每个任务都包含：

- 真实路径。
- 失败测试或回归测试。
- 最小实现说明。
- 验收标准。
- 验证命令。

建议入口：

```powershell
codepilot plan -p codepilot-dev "实现 Web UI 编程工作流任务板第一阶段：按 Backlog/Ready/Running/Review/Blocked/Done 展示任务，并保留详情、日志和任务操作入口" --json
```
