# 借鉴 oh-my-codex 的 CodePilot 改造计划

本文记录从 `oh-my-codex` 借鉴到 CodePilot 的功能方向、取舍边界和第一阶段任务拆分。

## 背景

CodePilot 当前优势在于本地工程任务队列、项目服务、Web UI、飞书和任务执行闭环。`oh-my-codex` 的优势在于 Codex 会话增强、明确的澄清/计划/执行/验证工作流、项目记忆、只读探索入口和运行时状态管理。

本轮不照搬 `oh-my-codex` 的 Node/Rust/tmux runtime，而是吸收它的工作流设计，用 CodePilot 现有 Python CLI、SQLite 存储、Web UI、daemon、inspect、Feishu/webhook 体系实现。

## 设计原则

- 保持 CodePilot 的跨 provider 定位，不把核心能力绑定到 Codex 专属 hooks。
- 先做可落地的工作流纪律：澄清、计划、只读探索、状态、记忆、诊断。
- 避免引入 Node/Rust sidecar 和 tmux 作为第一阶段依赖。
- 新能力优先暴露 CLI 和 JSON 输出，再接 Web UI/飞书。
- 复用现有数据库、任务模板、progress bus、web events、inspect signals 和 provider 配置。

## 功能路线

### 阶段一：工作流地基

目标：先让 CodePilot 拥有清晰的工作流状态、只读探索、项目记忆和更强环境诊断。

建议交付：

- 工作流状态存储：记录 `clarify`、`plan`、`run`、`review` 等模式的 active 状态、阶段、session、上下文路径。
- `codepilot explore`：只读查询项目文件、Git、任务日志、inspect 信号和已有任务，给规划器提供证据。
- `codepilot wiki`：轻量项目知识库，支持 add/list/query/lint，存放长期可复用事实。
- `codepilot doctor --project/--services`：扩展项目服务、配置、Provider、Web UI、Feishu、daemon、inspect 检查。
- `codepilot clarify` 和 `codepilot plan` 的最小闭环：先产出 spec/plan artifact，不直接执行。

### 阶段二：事件与通知统一

目标：把 Web UI、Feishu、webhook、日志和内部 progress bus 接到统一事件协议。

建议交付：

- 标准事件模型：`workflow_started`、`workflow_blocked`、`task_started`、`task_done`、`task_failed`、`review_failed`。
- Hook dispatcher：读取 `.codepilot/hooks/` 或配置中的 hook，按事件调用。
- 将已有 Feishu/webhook/Web UI 推送迁移到事件模型上。
- 对外提供 `codepilot events tail --json` 或服务日志 tail API。

### 阶段三：并行执行

目标：借鉴 `oh-my-codex` 的 team 思路，但不用 tmux。用 CodePilot 任务队列和 worktree/branch 隔离做可控并行。

建议交付：

- `codepilot run --parallel N`。
- 任务级锁、heartbeat、claim token。
- 任务间依赖检查。
- 全局并发限制和 provider 限流。
- Web UI 展示 worker/任务执行状态。

## 第一阶段边界

第一阶段不做：

- 不引入 tmux/psmux。
- 不引入 Rust native binary。
- 不实现真正并行执行。
- 不改动现有任务执行核心语义。
- 不迁移所有 Web UI/Feishu 事件，只为后续统一事件层预留接口。

第一阶段完成后，用户应该能：

1. 用 `codepilot explore` 获得项目证据。
2. 用 `codepilot wiki` 沉淀和查询项目知识。
3. 用 `codepilot clarify` 生成执行前 spec。
4. 用 `codepilot plan` 生成可审查计划。
5. 用扩展后的 `doctor` 判断项目服务和集成是否健康。

## 任务拆分

第一阶段任务文件：

```powershell
codepilot add -p codepilot-dev -f dev-bin/omx-borrow-phase1-tasks.md
```

如果当前 Python 环境缺少依赖，先安装项目依赖后再导入：

```powershell
python -m pip install -e .[dev]
```

