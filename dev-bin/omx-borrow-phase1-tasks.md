# 设计 CodePilot 工作流模式状态与 artifact 目录

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | dual |
| Priority | P0 |
| Depends on | - |
| Risk Level | Medium |
| Scope Budget | M |
| Owner | CodePilot |

---

## Task Goal

为第一阶段改造建立统一的工作流状态模型和 artifact 目录约定，让 `clarify`、`plan`、`explore`、`wiki` 等后续命令可以共享上下文、恢复状态并输出机器可读结果。

## In Scope

- 新增工作流状态模型，至少覆盖 mode、active、current_phase、session_id、context_path、artifact_paths、started_at、updated_at、completed_at。
- 设计 `.codepilot/state/`、`.codepilot/context/`、`.codepilot/specs/`、`.codepilot/plans/` 的目录约定，或等价数据库/文件混合方案。
- 提供读写状态的内部 API，保证原子写入和损坏文件容错。
- 增加最小 CLI 或内部命令用于查看当前 active workflow 状态。
- 写入中文设计文档，说明状态字段、目录语义、清理策略和与现有任务 DB 的关系。

## Out of Scope

- 不实现完整 `clarify`、`plan`、`wiki` 命令。
- 不迁移现有任务表结构，除非确有必要。
- 不引入 MCP server 或 Codex 专属状态协议。

## Forbidden (Hard Boundary)

- 不破坏现有 `codepilot status`、`task`、`run`、`daemon` 行为。
- 不把状态目录命名为 `.omx`。
- 不依赖 tmux、Node 或 Rust。

## Files In Scope

- `codepilot/core/`
- `codepilot/storage/`
- `codepilot/commands/`
- `docs/`
- `tests/`

## Planning Evidence

- CodePilot 已有 SQLite 任务状态和服务状态，但缺少跨工作流模式的状态层。
- `oh-my-codex` 的 `.omx/state/<mode>-state.json` 思路可借鉴，但需要按 CodePilot 定位重做。
- 当前任务模板要求每个任务有明确验收和验证矩阵。

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

- 如果引入数据库表，需考虑旧安装迁移。优先用文件状态降低迁移风险。
- Windows 文件替换需使用安全原子写策略，避免状态半写入。

---

## Acceptance Criteria

- [ ] 存在可复用的 workflow state 读写 API。
- [ ] 状态写入为原子写，损坏状态不会导致 CLI 崩溃。
- [ ] 文档说明状态字段和目录结构。
- [ ] 有测试覆盖创建、读取、更新、清理、损坏状态容错。

## Verification Matrix

| AC | Command | Expected Result | Evidence |
| :--- | :--- | :--- | :--- |
| 状态 API 测试 | `pytest tests -k workflow_state` | 新增测试通过 | 测试输出 |
| 回归测试 | `pytest tests` | 现有测试不回归 | 测试输出 |
| 文档检查 | 人工阅读新增 docs | 字段和目录约定清晰 | 文档路径 |

## Execution Order

1. Red: add or update the focused test that captures the target behavior; confirm it fails when feasible.
2. Green: implement the smallest change needed to pass that test and satisfy the happy path.
3. Refactor: clean only what is necessary inside this task's scope.
4. Verify: run every check in the verification matrix and record evidence.

---

## Reviewer Checkpoints

- 确认状态层没有影响现有任务 DB 行为。
- 确认文件写入在 Windows 下可用。
- 确认文档中的字段和实现一致。

- Only verify AC items for this task; do not expand to unrelated tech debt.
- Confirm nothing in "Forbidden" or "Out of Scope" was touched.
- Confirm the verification matrix has real evidence — verbal "verified" is not accepted.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.

---

## Rollback Strategy

- **Trigger** — 状态层导致现有 CLI 或任务执行异常。
- **Action** — 移除新命令入口，保留未接入的内部 API 或回退新增文件。
- **Fallback** — 仅保留设计文档，后续重新实现。

---

## Delivery Record

> Fill in after execution.

- **Completed at** —
- **Changed files** —
- **TDD evidence** —
- **Verification result** —
- **API changes** —
- **Review verdict** —
- **Risks & limitations** —
- **Next steps** —

---

# 新增 codepilot explore 只读项目探索入口

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | dual |
| Priority | P0 |
| Depends on | - |
| Risk Level | Medium |
| Scope Budget | M |
| Owner | CodePilot |

---

## Task Goal

新增 `codepilot explore`，提供安全的只读项目探索入口，用于查询文件、符号、Git 信息、任务日志、inspect 信号和项目状态，为澄清和计划阶段提供证据。

## In Scope

- 新增 `codepilot explore "<问题>"` 或 `codepilot explore --prompt "<问题>"` 命令。
- 支持 `--json` 输出，返回 query、evidence、sources、limitations。
- 首版只做安全只读操作：文件列表、文本搜索、Git log/status/show、任务日志摘要、inspect signal 读取。
- 对明显需要修改、执行测试、安装依赖的请求给出拒绝或转交普通 workflow 的提示。
- 将 explore 能力接入后续 `clarify` / `plan` 可复用 API。

## Out of Scope

- 不做 AI 自动回答的复杂推理闭环。
- 不执行修改类命令。
- 不引入 Rust sparkshell 或 native harness。
- 不支持 shell metacharacter 自由执行。

## Forbidden (Hard Boundary)

- 不允许 `explore` 写文件、改 Git、启动服务或执行测试。
- 不允许把用户 prompt 直接拼接进 shell 命令。
- 不依赖 `rg` 必须可用；Windows 下要有 fallback。

## Files In Scope

- `codepilot/commands/explore.py`
- `codepilot/cli.py`
- `codepilot/core/`
- `codepilot/storage/`
- `tests/`
- `docs/AI与Agent调用手册.zh-CN.md`

## Planning Evidence

- `oh-my-codex` 的 `omx explore` 把简单只读查询和执行型任务分开，能降低误执行风险。
- 当前环境里 `rg.exe` 启动曾出现拒绝访问，必须保留 PowerShell/Python fallback。
- CodePilot 已有 inspect signals、task logs、status，可作为 explore 证据源。

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

- 最大风险是把自然语言误判成 shell 命令。应先做白名单和结构化 evidence，不做自由命令执行。

---

## Acceptance Criteria

- [ ] `codepilot explore --prompt "..." --json` 可返回结构化证据。
- [ ] 修改类请求不会执行写操作，并返回明确限制。
- [ ] 文件搜索在 `rg` 不可用时仍有 fallback。
- [ ] 命令接入 `codepilot ai manifest` 或 AI 使用手册。
- [ ] 有测试覆盖只读查询、拒绝写操作、JSON 输出。

## Verification Matrix

| AC | Command | Expected Result | Evidence |
| :--- | :--- | :--- | :--- |
| explore JSON | `python -m codepilot explore --prompt "find task template" --json` | 输出含 evidence/sources | 命令输出 |
| 写操作拒绝 | `python -m codepilot explore --prompt "delete a file" --json` | 输出 rejected/unsupported | 命令输出 |
| 测试 | `pytest tests -k explore` | 新增测试通过 | 测试输出 |
| 回归 | `pytest tests` | 现有测试不回归 | 测试输出 |

## Execution Order

1. Red: add or update the focused test that captures the target behavior; confirm it fails when feasible.
2. Green: implement the smallest change needed to pass that test and satisfy the happy path.
3. Refactor: clean only what is necessary inside this task's scope.
4. Verify: run every check in the verification matrix and record evidence.

---

## Reviewer Checkpoints

- 确认所有数据源都是只读。
- 确认 prompt 不会被直接拼接进 shell。
- 确认 JSON 契约稳定且文档已更新。

- Only verify AC items for this task; do not expand to unrelated tech debt.
- Confirm nothing in "Forbidden" or "Out of Scope" was touched.
- Confirm the verification matrix has real evidence — verbal "verified" is not accepted.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.

---

## Rollback Strategy

- **Trigger** — explore 引入误执行风险或影响 CLI 启动。
- **Action** — 从 lazy command map 移除 `explore`，保留未暴露内部模块。
- **Fallback** — 暂时在文档中建议使用现有 `status`、`task logs`、`inspect`。

---

## Delivery Record

> Fill in after execution.

- **Completed at** —
- **Changed files** —
- **TDD evidence** —
- **Verification result** —
- **API changes** —
- **Review verdict** —
- **Risks & limitations** —
- **Next steps** —

---

# 新增轻量项目 wiki 与 memory 命令

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | dual |
| Priority | P1 |
| Depends on | - |
| Risk Level | Low |
| Scope Budget | M |
| Owner | CodePilot |

---

## Task Goal

实现轻量项目知识库，让 CodePilot 能沉淀构建命令、架构事实、巡检发现、常见失败和人工决策，并在后续 explore/clarify/plan 中优先检索这些本地知识。

## In Scope

- 新增 `.codepilot/wiki/` 或等价项目目录。
- 新增 `codepilot wiki add/list/query/lint`。
- Markdown-first 存储，支持 frontmatter 或简单元数据。
- `query` 支持关键词/CJK token 的基础检索，返回文件名、标题、摘要、分数。
- 支持从 inspect 结果或人工文本添加 note 的内部 API。
- 更新 AI 使用手册，说明哪些事实适合写入 wiki。

## Out of Scope

- 不做向量数据库。
- 不做远程同步。
- 不做复杂权限系统。
- 不自动改写大段源码或日志为 wiki，首版由命令显式写入。

## Forbidden (Hard Boundary)

- 不把 secrets、API key、Feishu app_secret 写入 wiki。
- 不默认上传或联网同步 wiki。
- 不读取仓库外任意路径作为 wiki 页面。

## Files In Scope

- `codepilot/commands/wiki.py`
- `codepilot/core/`
- `codepilot/cli.py`
- `docs/`
- `tests/`

## Planning Evidence

- OMX 的 wiki 是本地 markdown-first 项目知识层，适合借鉴。
- CodePilot 已有 inspect 和任务日志，但缺少可长期复用的项目知识沉淀。

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

- 需要避免把临时日志无限写入 wiki。首版保持显式 add，后续再考虑自动沉淀策略。

---

## Acceptance Criteria

- [ ] `codepilot wiki add --title ... --body ...` 创建页面。
- [ ] `codepilot wiki list --json` 返回页面列表。
- [ ] `codepilot wiki query "..." --json` 返回匹配结果。
- [ ] `codepilot wiki lint --json` 能发现空标题、非法路径或缺元数据。
- [ ] 有测试覆盖存储、查询、CJK 查询、路径越界保护。

## Verification Matrix

| AC | Command | Expected Result | Evidence |
| :--- | :--- | :--- | :--- |
| 新增页面 | `python -m codepilot wiki add --title "构建命令" --body "pytest tests"` | 页面写入成功 | 命令输出 |
| 查询页面 | `python -m codepilot wiki query "构建" --json` | 返回匹配页面 | 命令输出 |
| 测试 | `pytest tests -k wiki` | 新增测试通过 | 测试输出 |
| 回归 | `pytest tests` | 现有测试不回归 | 测试输出 |

## Execution Order

1. Red: add or update the focused test that captures the target behavior; confirm it fails when feasible.
2. Green: implement the smallest change needed to pass that test and satisfy the happy path.
3. Refactor: clean only what is necessary inside this task's scope.
4. Verify: run every check in the verification matrix and record evidence.

---

## Reviewer Checkpoints

- 确认路径越界被拒绝。
- 确认不会写入 secrets。
- 确认中文查询可用。

- Only verify AC items for this task; do not expand to unrelated tech debt.
- Confirm nothing in "Forbidden" or "Out of Scope" was touched.
- Confirm the verification matrix has real evidence — verbal "verified" is not accepted.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.

---

## Rollback Strategy

- **Trigger** — wiki 命令影响 CLI 或路径保护不可靠。
- **Action** — 移除 `wiki` CLI 注册，保留文档草案。
- **Fallback** — 暂用 docs 手工维护项目知识。

---

## Delivery Record

> Fill in after execution.

- **Completed at** —
- **Changed files** —
- **TDD evidence** —
- **Verification result** —
- **API changes** —
- **Review verdict** —
- **Risks & limitations** —
- **Next steps** —

---

# 扩展 doctor 支持项目服务和集成健康检查

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | dual |
| Priority | P1 |
| Depends on | - |
| Risk Level | Low |
| Scope Budget | S |
| Owner | CodePilot |

---

## Task Goal

增强 `codepilot doctor`，让它不仅检查 Python/Git/AGENTS/API key，还能检查项目级 daemon、inspect、Web UI、Feishu、webhook、任务 DB 和配置一致性。

## In Scope

- 增加 `codepilot doctor --project <name>`。
- 增加 `codepilot doctor --services` 或在 project 模式下展示服务健康。
- 检查 daemon/inspect pid、heartbeat、stop request、log path、stale meta。
- 检查 Web UI 服务状态和日志路径。
- 检查 Feishu 配置是否启用但缺 secret。
- JSON 输出保持现有 envelope 风格。
- 文档更新修复建议。

## Out of Scope

- 不自动启动服务。
- 不自动修复配置，除非只是提示。
- 不读取或打印 secret 明文。

## Forbidden (Hard Boundary)

- 不强杀任何进程。
- 不修改 AGENTS.toml 或 secrets 文件。
- 不把 warnings 当 fatal error，除非确实阻断当前配置。

## Files In Scope

- `codepilot/commands/doctor.py`
- `codepilot/commands/daemon.py`
- `codepilot/commands/inspect.py`
- `codepilot/commands/ui.py`
- `codepilot/commands/feishu.py`
- `tests/`
- `docs/操作文档.zh-CN.md`

## Planning Evidence

- CodePilot 已有 doctor，但覆盖范围偏环境级。
- 最近项目服务改造后，daemon/inspect/Web UI/Feishu 的健康状态对日常使用很关键。
- OMX 的 doctor 对 runtime shape 检查更完整，可借鉴诊断维度而非实现。

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

- 检查服务状态要复用现有 status API，避免重复实现产生不一致。

---

## Acceptance Criteria

- [ ] `codepilot doctor --project codepilot-dev --json` 返回项目健康检查。
- [ ] `codepilot doctor --services --json` 返回 daemon/inspect/Web UI/Feishu 检查结果。
- [ ] Feishu 启用但缺 secret 时返回 warning 和修复建议。
- [ ] stale PID/meta 不导致 doctor 崩溃。
- [ ] 有测试覆盖 JSON contract 和 warning/error 聚合。

## Verification Matrix

| AC | Command | Expected Result | Evidence |
| :--- | :--- | :--- | :--- |
| 项目检查 | `python -m codepilot doctor --project codepilot-dev --json` | 返回 project/service checks | 命令输出 |
| 服务检查 | `python -m codepilot doctor --services --json` | 返回服务 checks | 命令输出 |
| 测试 | `pytest tests -k doctor` | 新增测试通过 | 测试输出 |
| 回归 | `pytest tests` | 现有测试不回归 | 测试输出 |

## Execution Order

1. Red: add or update the focused test that captures the target behavior; confirm it fails when feasible.
2. Green: implement the smallest change needed to pass that test and satisfy the happy path.
3. Refactor: clean only what is necessary inside this task's scope.
4. Verify: run every check in the verification matrix and record evidence.

---

## Reviewer Checkpoints

- 确认 doctor 不改变系统状态。
- 确认 secret 不会出现在输出中。
- 确认旧 `codepilot doctor --json` 兼容。

- Only verify AC items for this task; do not expand to unrelated tech debt.
- Confirm nothing in "Forbidden" or "Out of Scope" was touched.
- Confirm the verification matrix has real evidence — verbal "verified" is not accepted.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.

---

## Rollback Strategy

- **Trigger** — doctor 输出破坏现有调用方。
- **Action** — 保留旧默认输出，关闭新选项注册或修复 JSON envelope。
- **Fallback** — 先以独立 `doctor_services` 内部函数保留实现。

---

## Delivery Record

> Fill in after execution.

- **Completed at** —
- **Changed files** —
- **TDD evidence** —
- **Verification result** —
- **API changes** —
- **Review verdict** —
- **Risks & limitations** —
- **Next steps** —

---

# 新增 clarify 命令生成执行前需求规格

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | dual |
| Priority | P1 |
| Depends on | - |
| Risk Level | Medium |
| Scope Budget | M |
| Owner | CodePilot |

---

## Task Goal

新增 `codepilot clarify`，把模糊需求转成可执行规格文档，在不直接创建任务或执行代码的前提下明确目标、范围、非目标、约束、验收标准和待确认问题。

## In Scope

- 新增 `codepilot clarify "<需求>"`。
- 支持 `--quick`、`--standard`、`--json`。
- 生成 `.codepilot/specs/clarify-<slug>.md` 或等价 artifact。
- 首版可使用本地规则和可选 planner provider；provider 不可用时生成结构化待确认问题。
- 对 brownfield 请求优先调用 explore API 收集项目证据。
- 写入 workflow state，标记 clarify active/complete。

## Out of Scope

- 不做多轮交互 UI 的完整实现。
- 不自动执行 `codepilot add`。
- 不要求一次性解决所有歧义。

## Forbidden (Hard Boundary)

- `clarify` 不得修改业务代码。
- `clarify` 不得启动 daemon/run。
- 未明确确认前不得创建任务 backlog。

## Files In Scope

- `codepilot/commands/clarify.py`
- `codepilot/ai_support/`
- `codepilot/core/`
- `codepilot/cli.py`
- `tests/`
- `docs/AI与Agent调用手册.zh-CN.md`

## Planning Evidence

- CodePilot 已有 clarification protocol 和交互控制模块，可复用。
- OMX 的 deep-interview 强调非目标和决策边界，适合转化为 CodePilot 的执行前 spec。

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

- 不要把 clarify 变成另一个自动执行入口。它的交付物是 spec，不是任务执行。

---

## Acceptance Criteria

- [ ] `codepilot clarify "..." --json` 输出 artifact path、summary、open_questions。
- [ ] 生成的 spec 包含目标、范围、非目标、约束、验收标准、待确认问题。
- [ ] brownfield 请求会包含 explore 证据或明确说明未收集到。
- [ ] provider 不可用时仍能产出规则化 spec skeleton。
- [ ] 有测试覆盖 artifact 生成和不创建任务。

## Verification Matrix

| AC | Command | Expected Result | Evidence |
| :--- | :--- | :--- | :--- |
| 生成 spec | `python -m codepilot clarify "改进 doctor" --json` | 返回 spec path | 命令输出和文件 |
| 不创建任务 | `python -m codepilot status -p codepilot-dev --json` | 任务数不因 clarify 增加 | 前后对比 |
| 测试 | `pytest tests -k clarify` | 新增测试通过 | 测试输出 |
| 回归 | `pytest tests` | 现有测试不回归 | 测试输出 |

## Execution Order

1. Red: add or update the focused test that captures the target behavior; confirm it fails when feasible.
2. Green: implement the smallest change needed to pass that test and satisfy the happy path.
3. Refactor: clean only what is necessary inside this task's scope.
4. Verify: run every check in the verification matrix and record evidence.

---

## Reviewer Checkpoints

- 确认 clarify 没有创建任务、启动执行或修改业务代码。
- 确认 spec 的非目标和决策边界明确。
- 确认 provider 缺失 fallback 可用。

- Only verify AC items for this task; do not expand to unrelated tech debt.
- Confirm nothing in "Forbidden" or "Out of Scope" was touched.
- Confirm the verification matrix has real evidence — verbal "verified" is not accepted.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.

---

## Rollback Strategy

- **Trigger** — clarify 误创建任务或阻塞现有自然语言入口。
- **Action** — 从 CLI lazy map 移除 `clarify`。
- **Fallback** — 保留内部 spec builder 供后续 plan 使用。

---

## Delivery Record

> Fill in after execution.

- **Completed at** —
- **Changed files** —
- **TDD evidence** —
- **Verification result** —
- **API changes** —
- **Review verdict** —
- **Risks & limitations** —
- **Next steps** —

---

# 新增 plan 命令生成可审查执行计划

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | dual |
| Priority | P1 |
| Depends on | - |
| Risk Level | Medium |
| Scope Budget | M |
| Owner | CodePilot |

---

## Task Goal

新增 `codepilot plan`，从需求文本或 clarify spec 生成可审查的执行计划和任务候选，但默认不执行、不入 backlog，作为任务创建前的审批层。

## In Scope

- 新增 `codepilot plan "<需求>"` 和 `codepilot plan --from-spec <path>`。
- 输出 `.codepilot/plans/plan-<slug>.md`。
- 支持 `--json` 返回 plan path、task candidates、risks、verification plan。
- 复用现有 task planning/prompt 能力，但把输出限定为计划 artifact。
- 明确后续桥接命令建议：导入任务、继续 clarify、或直接放弃。

## Out of Scope

- 不直接执行任务。
- 不默认写入 backlog。
- 不实现完整 UI 审批流。

## Forbidden (Hard Boundary)

- 没有显式 `--create-tasks` 或后续确认时，不调用 `db.create_task`。
- 不修改业务代码。
- 不启动 run/daemon。

## Files In Scope

- `codepilot/commands/plan.py`
- `codepilot/ai_support/task_planning.py`
- `codepilot/ai_support/planner_*`
- `codepilot/core/`
- `codepilot/cli.py`
- `tests/`
- `docs/AI与Agent调用手册.zh-CN.md`

## Planning Evidence

- CodePilot 已有自然语言转任务能力，但缺少“先计划再审批”的显式中间层。
- OMX 的 `ralplan` 思路可转化为 CodePilot 的 plan artifact。

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

- 要避免和现有 `go` 自动创建任务语义混淆。`plan` 必须是审查层。

---

## Acceptance Criteria

- [ ] `codepilot plan "..." --json` 返回计划 artifact 和候选任务。
- [ ] `codepilot plan --from-spec <path> --json` 能消费 clarify spec。
- [ ] 默认不会创建 backlog 任务。
- [ ] 计划包含执行顺序、文件范围、风险、验证矩阵。
- [ ] 有测试覆盖从文本和 spec 生成计划、不创建任务。

## Verification Matrix

| AC | Command | Expected Result | Evidence |
| :--- | :--- | :--- | :--- |
| 文本计划 | `python -m codepilot plan "新增 explore" --json` | 返回 plan path | 命令输出和文件 |
| spec 计划 | `python -m codepilot plan --from-spec .codepilot/specs/example.md --json` | 返回 plan path | 命令输出 |
| 不创建任务 | `python -m codepilot status -p codepilot-dev --json` | 任务数不因 plan 增加 | 前后对比 |
| 测试 | `pytest tests -k plan_command` | 新增测试通过 | 测试输出 |

## Execution Order

1. Red: add or update the focused test that captures the target behavior; confirm it fails when feasible.
2. Green: implement the smallest change needed to pass that test and satisfy the happy path.
3. Refactor: clean only what is necessary inside this task's scope.
4. Verify: run every check in the verification matrix and record evidence.

---

## Reviewer Checkpoints

- 确认默认没有任务入库。
- 确认计划 artifact 对后续任务拆分足够具体。
- 确认 JSON 输出稳定。

- Only verify AC items for this task; do not expand to unrelated tech debt.
- Confirm nothing in "Forbidden" or "Out of Scope" was touched.
- Confirm the verification matrix has real evidence — verbal "verified" is not accepted.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.

---

## Rollback Strategy

- **Trigger** — plan 命令误创建任务或与 go 行为冲突。
- **Action** — 移除 CLI 注册，保留 plan artifact builder。
- **Fallback** — 继续使用现有自然语言入口生成任务。

---

## Delivery Record

> Fill in after execution.

- **Completed at** —
- **Changed files** —
- **TDD evidence** —
- **Verification result** —
- **API changes** —
- **Review verdict** —
- **Risks & limitations** —
- **Next steps** —

