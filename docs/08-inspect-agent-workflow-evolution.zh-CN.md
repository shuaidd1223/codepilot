# inspect 与智能体工作流演进分析

维护日期：2026-05-22

本文用于承接本轮会话中关于“从工作流辅助工具演进到智能体 + 工作流结合形态”的讨论和实现结果。新会话可以直接读取本文继续推进，不需要重新还原上下文。

## 当前结论

本轮已经完成 `inspect` 质量优化的前三个阶段，并已构建、安装、验证最新版二进制。当前没有待执行、执行中或失败的 CodePilot 任务。

但“智能体 + 工作流结合”的长期目标尚未完成。当前完成的是其中 `inspect` 作为自动发现入口的质量基线，后续仍需要把发现、解释、人工反馈、工作流推进和智能体执行闭环连起来。

## 本轮已完成

### 阶段一：收紧 inspect 任务生成质量

对应提交：`26e0586 task #355: 收紧 inspect 第一阶段任务生成质量`

核心变化：

- JSON 模式下减少非结构化噪声。
- `pytest collect` 成功时不再把大量成功输出塞进规划上下文。
- `code_metrics` 拆分生产代码和测试代码信号，避免测试代码指标误导任务生成。
- 候选项必须绑定真实文件路径。
- 生成任务内容必须包含文件、验收标准和验证命令。

涉及文件：

- `codepilot/ai_support/task_planning.py`
- `codepilot/commands/inspect.py`
- `codepilot/commands/inspect_signal_collectors.py`
- `codepilot/commands/inspect_signal_collectors_code_metrics.py`
- `tests/test_inspect_code_metrics.py`
- `tests/test_inspect_flow_split.py`
- `tests/test_inspect_pytest_collect.py`

### 阶段二：增加 report-only 候选分层

对应提交：`a56d9d8 task #356: 为 inspect 增加 report-only 候选分层`

核心变化：

- 新增 `report_only` 和 `report_only_count`。
- P4、只有弱信号、仅来自 benign git log 或低价值 code metrics 的候选不再直接创建任务。
- 仍保留强信号候选进入任务创建路径，避免过度过滤。

涉及文件：

- `codepilot/commands/inspect.py`
- `tests/test_inspect_code_metrics.py`
- `tests/test_inspect_flow_split.py`

### 阶段三：增加质量摘要和决策链

对应提交：`b4ef8d6 task #357: 为 inspect 输出质量摘要和决策链`

核心变化：

- JSON 输出新增 `quality_summary`。
- 能看到本轮 created、skipped、dropped、report_only 的数量。
- 能看到各类过滤、跳过、降级原因分布。
- 覆盖正常路径、提前退出路径和错误路径。

涉及文件：

- `codepilot/commands/inspect.py`
- `tests/test_inspect_flow_split.py`

### 验证和安装状态

已完成验证：

```powershell
pytest -n auto --dist loadfile -m "not slow" -q
```

结果：`1575 passed`

已完成二进制构建、安装和验证：

```powershell
python -m codepilot binary build
python -m codepilot binary install --binary "D:\myCode\workflow\dist\binary\windows-x86_64\codepilot.exe"
python -m codepilot binary verify
```

安装版本路径：

```text
C:\Users\Administrator\AppData\Local\Programs\CodePilot\bin\codepilot.exe
```

后续做运行态验证、状态查询、巡检试跑时，优先使用这个安装版本，避免本地源码改动影响当前工作流服务。

## 当前能力边界

`inspect` 现在更像一个“带质量门禁的候选发现器”，已经能减少低质量任务进入队列，但还不是完整智能体闭环。

已经具备：

- 从项目中采集多类信号。
- 根据强弱信号决定是否创建任务。
- 将低置信度建议降级到报告区。
- 输出本轮质量摘要和决策原因。
- 支持 `--dry-run --json` 作为机器可读评估入口。
- 根据项目本地 memory feedback 对候选自动升权/降权，并在 `quality_summary.feedback_adjusted` 暴露调整记录。

仍然缺少：

- Web UI 对 `report_only` / `quality_summary` 的清晰展示。
- 人工从报告项一键提升为任务的路径。
- 基于人工删除、归档、重试结果的更细粒度反馈学习。
- 与 Agent Kernel 会话阶段的深度绑定。
- 跨轮次质量评测和回放基准。

## 后续阶段建议

### 阶段四：Web UI 展示巡检质量和报告项

目标：让用户在 Web UI 中能看懂 `inspect` 为什么创建或不创建任务。

建议任务拆分：

1. 在巡检结果或项目状态视图中展示 `quality_summary`。
2. 展示 `report_only` 列表，区分“已创建任务”和“仅报告建议”。
3. 每个报告项展示标题、原因、信号来源、相关文件、建议验证命令。
4. 为报告项预留“提升为任务”的交互入口，但第一步可以只做展示。

验收标准：

- `inspect --dry-run --json` 输出中的 `report_only` 和 `quality_summary` 能被 Web UI 消费。
- UI 中不会把 report-only 项误显示为 backlog 任务。
- 空结果、错误结果、只有 report-only、既有任务又有 report-only 都有合理展示。

建议验证：

```powershell
& "C:\Users\Administrator\AppData\Local\Programs\CodePilot\bin\codepilot.exe" inspect -p codepilot-dev --once --dry-run --json
pytest -n auto --dist loadfile -m "not slow" -q tests/test_inspect_flow_split.py
```

风险边界：

- 不要为了展示改动任务队列表结构。
- 不要让 `--dry-run` 产生数据库写入。
- 不要把低置信度建议自动入队。

### 阶段五：人工反馈闭环

目标：让系统记住哪些 inspect 建议被用户认为无用，并在后续降权。

建议任务拆分：

1. 定义 inspect 候选的稳定指纹，例如 `source + normalized title + file set + reason`。
2. 记录用户对巡检项的处理结果：删除、归档、提升、执行成功、执行失败。
3. 下一轮 inspect 根据历史结果调整候选评分或降级到 report-only。
4. 输出 `quality_summary.feedback_adjusted`，说明哪些候选因为历史反馈被调整。

验收标准：

- 同类低价值建议被多次删除或归档后，不再反复创建任务。
- 被用户手动提升并成功执行的建议，后续同类信号不被错误压制。
- 反馈逻辑有测试覆盖，且不影响无历史数据项目。

建议验证：

```powershell
pytest -n auto --dist loadfile -m "not slow" -q tests/test_inspect_flow_split.py
```

风险边界：

- 反馈数据应是项目本地状态，不要写入用户级全局配置。
- 指纹不要包含绝对时间、临时路径等不稳定字段。
- 反馈只影响排序和分层，不应直接跳过高风险强信号。

### 阶段六：接入 Agent Kernel 工作流阶段

目标：让 inspect 不只是“扫问题”，而是能驱动 `intake -> explore -> plan -> execute -> review` 的工作流推进。

建议任务拆分：

1. 将 inspect 结果作为 Agent Session 的 evidence 输入。
2. 当存在高置信度候选时，生成明确的 `next_actions`，例如“生成计划”“导入任务”“继续探索”。
3. 对 report-only 项生成“继续观察”或“人工确认”的工作流动作。
4. 在 `workflow status` 中暴露最近一次 inspect 的摘要和可执行下一步。

验收标准：

- 用户或外部 AI 可以通过 `workflow next --list --json` 看见安全推进动作。
- `suggested_command` 仍只用于展示，不作为自动执行源。
- 工作流推进必须走 allowlist action，不接受任意命令注入。

建议验证：

```powershell
pytest -n auto --dist loadfile -m "not slow" -q tests/test_workflow*.py tests/test_inspect_flow_split.py
```

风险边界：

- 不要让自由文本自动创建任务，仍应遵守显式动作入口。
- 不要绕过现有 `workflow next` 的安全 allowlist。
- 不要把 Agent Session 状态和任务队列表强耦合。

### 阶段七：建立 inspect 质量评测集

目标：用固定样本评估巡检质量，而不是只凭单次输出主观判断。

建议任务拆分：

1. 构造少量 fixture 项目或历史输出样本，覆盖强信号、弱信号、噪声信号、无信号。
2. 建立 golden summary：期望创建数量、report-only 数量、drop 原因。
3. 增加回放命令或测试辅助函数，确保规则调整不会让低质量任务回流。
4. 在文档中记录 inspect 质量指标，例如创建率、降级率、误报样例。

验收标准：

- 调整 inspect 策略时能快速知道是否提高或降低了任务质量。
- 评测不依赖真实 LLM 返回，避免测试不稳定。
- 样本小而可维护，不引入大型生成产物。

建议验证：

```powershell
pytest -n auto --dist loadfile -m "not slow" -q tests/test_inspect*.py
```

风险边界：

- 不要把完整项目源码复制到 fixture。
- 不要让评测依赖网络、真实服务或当前工作区偶然状态。

## 新会话接续步骤

建议新会话第一步先确认基线：

```powershell
git status --short
& "C:\Users\Administrator\AppData\Local\Programs\CodePilot\bin\codepilot.exe" status -p codepilot-dev --json
& "C:\Users\Administrator\AppData\Local\Programs\CodePilot\bin\codepilot.exe" inspect -p codepilot-dev --once --dry-run --json
& "C:\Users\Administrator\AppData\Local\Programs\CodePilot\bin\codepilot.exe" inspect -p codepilot-dev --once --dry-run --write-workflow --json
```

然后从阶段四开始做。推荐的新会话起始需求：

```text
继续根据 docs/inspect-agent-workflow-evolution.zh-CN.md 推进阶段四：
让 Web UI 展示 inspect 的 quality_summary 和 report_only，并保持 report-only 不自动入队。
按 AGENTS.md 的 TDD 要求先补测试，再做最小实现，最后跑相关测试。
```

## 不建议继续推进的方向

- 不要继续做泛化的“拆分文件”或清理类任务，除非它直接服务于上述阶段目标。
- 不要把低置信度巡检建议重新变成 backlog 任务。
- 不要为了智能体化引入新的外部 Agent 依赖；当前重点是把 CodePilot 自己的工作流、状态、反馈和执行闭环打通。
- 不要让 UI、飞书或自由文本绕过结构化的 `clarify`、`plan`、`workflow next` 和任务模板约束。

## 本轮落地记录

- `inspect --once --dry-run --write-workflow --json` 已作为低风险自动入口：只写 workflow context / Agent Session，不写 backlog。
- `workflow next` 已支持 inspect allowlist 动作：`create_inspect_tasks`、`promote_inspect_report_<candidate_id>`、`ignore_inspect_report_<candidate_id>`、`delete_inspect_report_<candidate_id>`、`archive_inspect_report_<candidate_id>`、`plan_from_inspect`，仍不执行 `suggested_command` 字符串。
- CLI、Web API、Web UI、MCP、Chat、飞书统一调用共享 inspect workflow core，避免各入口各自维护状态机。
- `candidate_id` 使用信号、标题、文件集合、reason 的稳定摘要，不使用时间、绝对项目路径或临时字段。

## 分阶段推进：项目记忆与自我进化

### 阶段一：事实观察日志

状态：已落地。

- 新增 `.codepilot/memory/events.jsonl` 作为项目本地自动观察事实日志。
- 自动从事实事件生成去重候选，并维护 `.codepilot/memory/autocapture.md`。
- `inspect --write-workflow` 写入 `inspect.workflow_context_written` 事件。
- `workflow next --action ...` 写入 `workflow.action_executed` 事件。
- `trace` 默认合并 memory events，`codepilot memory events -p <项目名> --json` 可单独查看。
- 这一层不要求人工 promote；它会自动沉淀低风险事实摘要，但不直接写人工维护的长期 wiki/note，避免记忆污染。

### 阶段二：自动评分与执行反馈回流

状态：已落地。

- memory candidate 新增 `feedback`、`score`、`signals`、`seen_count`、`first_seen_at`、`last_seen_at`、`source_event_ids`。
- `workflow.action_executed` 会按动作类型自动升权：提升 report-only、创建 inspect tasks、从 inspect 生成 plan 都会记录为正向反馈。
- 任务进入 `done` / `failed` / `cancelled` 终态时，数据库更新路径会自动写入 `task.updated` memory event。
- `done` 任务会沉淀为正向反馈，`failed` / `cancelled` 会沉淀为负向反馈，后续可用于 inspect 候选排序和降级。
- 重复事实不会重复生成候选，而是更新 `seen_count`、`last_seen_at` 和分数；`.codepilot/memory/autocapture.md` 会显示 score、feedback 和 seen 数。

### 阶段三：反馈接入 inspect 分层

状态：已落地。

- `run_inspection` 会读取项目本地 memory candidates，不存在历史反馈时保持原语义。
- 正向高分反馈会把同一 report-only 候选提升为 actionable，例如用户曾提升过的 `promote_inspect_report_<candidate_id>`。
- 负向低分反馈会把同标题 actionable 候选降级到 report-only，reason 为 `memory_negative_feedback`。
- `quality_summary.feedback_adjusted` 记录 `candidate_id`、标题、from/to、score、feedback 和 memory candidate 来源。
- 决策链新增可选 `memory_feedback` 阶段，仅在确实发生调整时出现。

### 阶段四：任务操作反馈统一接入 memory

状态：已落地。

- 任务归档会写入 `task.archived` memory event，作为正向反馈，表示完成结果被接受。
- 任务删除会写入 `task.deleted` memory event，作为负向反馈，后续同标题巡检候选会更容易降级到 report-only。
- 任务重试会写入 `task.retried` memory event，作为中等正向反馈，表示该方向仍值得继续尝试，但不会直接把 report-only 提升为任务。
- 这些事件下沉在 `storage.database` 层，CLI、Web UI、批量操作、飞书/MCP 只要复用同一任务操作路径，就会自动进入反馈闭环。

### 阶段五：report-only 显式反馈 action

状态：已落地。

- `workflow next` 会为 report-only 候选暴露 `ignore_inspect_report_<candidate_id>`、`delete_inspect_report_<candidate_id>`、`archive_inspect_report_<candidate_id>`。
- 这些动作只更新 inspect context 和 Agent Session，不创建 backlog 任务，也不执行候选里的 `suggested_command`。
- 每次处理都会写入 `inspect.report_feedback` memory event，并把反馈合并到项目本地 memory candidate，用于后续巡检降权。
- Web UI、CLI、Chat、飞书和 MCP 只需要调用同一个 workflow action handler，就能获得一致的忽略、删除、归档语义。

### 阶段六：巡检工作台反馈交互

状态：已落地。

- Web dashboard payload 返回 `ignored_report_only`、`deleted_report_only`、`archived_report_only`，前端可以展示已处理报告项。
- ProjectView 顶部只保留全局 workflow 动作，例如运行巡检、创建任务、生成计划；每个 report-only 项自身展示提升、忽略、归档、删除按钮。
- Chat 和飞书工作流卡片也会列出同一批 action id，按钮仍然只调用 `workflow next`，不拼接执行 `suggested_command`。
- 这样 report-only 从“静态报告列表”变成可反馈的工作台项，用户的每次处理都会自动进入 memory 闭环。

### 阶段七：低风险自动策略层

状态：已落地。

- `workflow next --auto --json` 会从当前 workflow context 中选择策略允许的自动动作。
- 默认策略优先自动忽略已有负反馈的重复 report-only；没有这类项时，会对 inspect context 自动执行一次 `plan_from_inspect`。
- 默认不会执行 `create_inspect_tasks`、`promote_inspect_report_<candidate_id>`、`import_tasks` 或任何高风险动作，也不会执行 `suggested_command` 字符串。
- 项目可在 `[automation]` 中用 `workflow_auto_create_inspect_tasks`、`workflow_auto_import_plan_tasks`、`workflow_auto_max_steps` 和 `workflow_auto_failure_threshold` 放开自动创建/导入、连续步数和失败熔断阈值。
- Web UI 增加“自动推进”入口，Web API 的 `/api/workflow/actions` 支持 `{"auto": true}`，MCP `workflow_next` 支持 `auto=true`，Chat/飞书暴露 `workflow next <project> auto`，全部复用同一个 policy handler。
