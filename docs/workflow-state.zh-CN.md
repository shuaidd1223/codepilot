# CodePilot 工作流模式状态与 artifact 目录约定

语言版本：中文 | [English](workflow-state.en-US.md)

本文定义 CodePilot 第一阶段工作流命令共享的项目本地状态层。该层服务于后续 `clarify`、`plan`、`explore`、`wiki` 等模式，让它们可以复用上下文、恢复状态，并输出机器可读 artifact。

## 设计目标

- 状态存储独立于现有任务队列 DB，避免迁移 `tasks` 表。
- 每个项目使用项目根目录下的 `.codepilot/` 保存工作流状态和 artifact。
- 状态文件使用 JSON，写入必须走同目录临时文件加 `os.replace`，避免半写入。
- 损坏 JSON 只视为无状态，不让 CLI 崩溃。

## 目录语义

项目根目录下使用以下目录：

| 路径 | 用途 | 清理策略 |
| :--- | :--- | :--- |
| `.codepilot/state/` | 工作流模式状态 JSON。包含 active 指针和每个 mode 的最近状态。 | 可清理 inactive 且已完成的 `*-state.json`。 |
| `.codepilot/context/` | 会话上下文快照，例如澄清问答、输入摘要、证据索引。 | 默认保留；后续可按 session 或时间归档。 |
| `.codepilot/specs/` | `clarify` 等命令产出的规格说明 artifact。 | 默认保留，作为执行前审查依据。 |
| `.codepilot/plans/` | `plan` 命令产出的计划 artifact。 | 默认保留，作为任务拆分和审查依据。 |

状态文件命名：

- `.codepilot/state/active-workflow.json`：当前 active workflow 指针，内容为完整状态快照。
- `.codepilot/state/<mode>-state.json`：某个模式最近一次状态，例如 `clarify-state.json`、`plan-state.json`。

禁止使用 `.omx` 作为状态目录名。

## 状态字段

当前内部 API 写入的状态字段如下：

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `mode` | string | 工作流模式，例如 `clarify`、`plan`、`explore`、`wiki`。 |
| `active` | boolean | 是否为仍在进行的工作流。`true` 时同步写入 `active-workflow.json`。 |
| `current_phase` | string | 当前阶段，例如 `started`、`collecting`、`questions_ready`、`completed`。 |
| `session_id` | string | 工作流会话 ID，用于关联 context/spec/plan artifact。 |
| `context_path` | string | 当前上下文快照路径，默认在 `.codepilot/context/<session_id>.json`。 |
| `artifact_paths` | object | 机器可读 artifact 路径集合，默认包含 `context`、`spec`、`plan`。 |
| `started_at` | string | ISO 时间戳，工作流开始时间。 |
| `updated_at` | string | ISO 时间戳，最近写入时间。 |
| `completed_at` | string/null | 完成时间；未完成时为 `null`。 |

## 内部 API

实现位于 `codepilot/core/workflow_state.py`：

- `workflow_dirs(project_path)`：返回目录约定。
- `ensure_workflow_dirs(project_path)`：创建 `.codepilot/state|context|specs|plans`。
- `start_workflow(...)`：创建 active 状态，并写入 active 指针和 mode 状态文件。
- `read_workflow_state(project_path, mode=None)`：读取 active 状态或指定 mode 状态；文件不存在或 JSON 损坏时返回 `None`。
- `update_workflow_state(project_path, mode, **changes)`：更新指定 mode 状态，保留 `started_at`，刷新 `updated_at`。
- `complete_workflow(project_path, mode)`：将状态标记为 inactive/completed，并清除对应 active 指针。
- `cleanup_workflow_states(project_path, completed=True)`：删除 inactive 且已完成的 mode 状态文件。

## CLI 查看

最小查看命令：

```powershell
codepilot workflow status -p <项目名>
codepilot workflow status -p <项目名> --json
codepilot workflow status -p <项目名> --mode clarify --json
```

JSON 输出沿用命令 envelope：

```json
{
  "ok": true,
  "command": "workflow status",
  "data": {
    "project": "demo",
    "project_path": "D:\\myCode\\demo",
    "mode": null,
    "state": {
      "mode": "clarify",
      "active": true
    }
  }
}
```

## 与现有任务 DB 的关系

工作流状态层不替代现有 SQLite 任务状态：

- SQLite DB 继续负责项目注册、任务队列、任务执行状态、日志索引和会话消息。
- `.codepilot/state/` 只保存跨工作流模式的轻量运行态和 artifact 路径。
- `clarify`、`plan` 等后续命令可先写 project-local artifact，再决定是否创建任务。
- 不需要迁移现有任务表；旧项目没有 `.codepilot/` 时，读取状态返回空状态。

这个分层可以让工作流命令在不影响 `codepilot status`、`task`、`run`、`daemon` 的前提下逐步落地。
