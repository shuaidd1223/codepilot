# Task #167 侦察报告（结构化）

## current_state

- 项目当前队列中已存在一批“拆分重构”待办（`#161/#162/#163/#165/#166`），说明方向已确认，但输入粒度不足，导致 `#167` 作为阻塞信号任务进入 builder。
- 执行链路的职责边界已开始拆分：`run.py` 主要做入口与兼容导出，`run_orchestrator.py` 负责队列编排，`run_builtin.py` 负责内置执行轮次，`run_shell.py` 负责 shell/live 流式执行细节。
- 巡检链路也已拆分：`inspect.py` 负责主流程与编排，`inspect_signals.py` 负责信号采集与聚合。

## relevant_files

- `codepilot/commands/run_shell.py:48` `PreflightSkipError` 定义为“预检跳过并回队”控制信号。
- `codepilot/commands/run_shell.py:124` `_run_command` 统一运行子命令。
- `codepilot/commands/run_shell.py:767` `_run_command_live` 处理 live 输出流与运行态。

- `codepilot/commands/run_builtin.py:29` 从 `run_shell` 引入 `PreflightSkipError`。
- `codepilot/commands/run_builtin.py:524` `_run_builtin_phase` 运行单阶段 agent。
- `codepilot/commands/run_builtin.py:860` `_run_builtin_round_loop` 管理 builder/reviewer 回合。
- `codepilot/commands/run_builtin.py:1000` `_run_builtin_executor` 聚合执行结果并映射状态。

- `codepilot/commands/run.py:53` 导入 `run_shell` 能力。
- `codepilot/commands/run.py:89` 导入 `run_builtin` 能力。
- `codepilot/commands/run.py:526` `run_backlog` 主循环入口（兼容层）。

- `codepilot/commands/run_orchestrator.py:204` 调用 `runner._run_builtin_executor`。
- `codepilot/commands/run_orchestrator.py:589` 捕获 `PreflightSkipError` 并执行 requeue 路径。

- `codepilot/commands/inspect_signals.py:28` `InspectSignalSpec` 描述信号定义。
- `codepilot/commands/inspect_signals.py:111` `collect_signal_results` 聚合所有启用信号。
- `codepilot/commands/inspect_signals.py:157` `collect_git_log`。
- `codepilot/commands/inspect_signals.py:351` `collect_todos`。
- `codepilot/commands/inspect_signals.py:469` `collect_dependency_health`。
- `codepilot/commands/inspect_signals.py:589` `collect_code_metrics`。

- `codepilot/commands/inspect.py:22` 导入 `inspect_lifecycle/inspect_service/inspect_signals`。
- `codepilot/commands/inspect.py:215` `INSPECT_SIGNAL_SPECS` 注册信号规格。
- `codepilot/commands/inspect.py:275` `_inspect_signal_collectors` 映射 collector。
- `codepilot/commands/inspect.py:287` `collect_inspection_signal_results` 统一聚合入口。
- `codepilot/commands/inspect.py:457` `run_inspection` 执行巡检主流程。

- `codepilot/web/components/AgentLog.js:42` 组件依赖 `parseMarkdownBlocks`。
- `codepilot/web/components/AgentLog.js:56` 组件依赖 `findSearchMatches`。
- `codepilot/web/components/AgentLog.js:223` 组件依赖 `scheduleEnhance`。
- `codepilot/web/boundaries/AgentLogRenderBoundary.js:217` `parseMarkdownBlocks` 实现。
- `codepilot/web/boundaries/AgentLogRenderBoundary.js:474` `scheduleEnhance` 实现。
- `codepilot/web/boundaries/AgentLogInteractionBoundary.js:7` `findSearchMatches` 实现。
- `codepilot/web/boundaries/AgentLogInteractionBoundary.js:68` `toggleFollow` 实现。

## key_findings

- `run` 执行链路已具备“入口/编排/执行/shell”分层雏形，但 `run_builtin.py` 与 `run_shell.py` 仍是高复杂度热点，适合继续纵向细分。
- `inspect` 链路已把信号采集抽离到 `inspect_signals.py`，下一步适合按“信号定义/collector 注册/聚合输出”继续切原子任务。
- AgentLog 前端已分 boundary，但组件与边界文件之间仍有高频耦合调用，适合按“渲染缓存、搜索交互、滚动状态”再拆。

## risks

- 若任务不约束到具体文件和调用点，规划器会重复产出“泛化重构任务”，可执行性弱。
- `PreflightSkipError` / requeue 语义跨 `run_shell -> run_builtin -> run_orchestrator`，拆分时若漏掉映射可能引入队列状态回归。
- AgentLog 拆分若未保留现有交互契约（搜索、跟随滚动、代码块增强），容易产生 UI 行为退化。

## dependencies_and_callpoints

- 执行链路依赖关系：`run.py -> run_orchestrator.py -> run_builtin.py -> run_shell.py`。
- 巡检链路依赖关系：`inspect.py -> inspect_signals.py`（spec + collector + result 聚合）。
- 前端依赖关系：`AgentLog.js -> AgentLogRenderBoundary.js / AgentLogInteractionBoundary.js`。

## suggested_approach

- 规划器按三条主线拆分：执行链路、巡检链路、AgentLog 前端边界。
- 每条主线先拆“接口/契约稳定层”，再拆“实现细节层”，确保每个子任务可单轮交付并可测试。
- 新任务正文必须携带 `file_path:line` 证据与回归验证点，防止再次进入空输入规划。
