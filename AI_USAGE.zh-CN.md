# CodePilot AI 调用手册

语言版本：中文 | [English](AI_USAGE.en-US.md)

**作者：** 帅呆呆 <2264505396@qq.com> | **仓库：** https://gitee.com/shuai_dd/CodePilot | **许可：** MIT

这份手册是写给其他 AI / Agent 的静态入口。最新机器可读清单以 `codepilot ai manifest` 为准，最新 Markdown 手册以 `codepilot ai guide` 为准。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时优先使用 `--json` 或 `codepilot ai manifest`。
3. 提交高层需求时直接调用 `codepilot "需求文本"` 或 `codepilot go "需求文本"`。
4. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态由智能体选择 CodePilot status/task/trace 工具读取。
5. `chat`、Web UI 会话和飞书自由文本统一进入 OpenCode 智能体并带上 CodePilot MCP 工具；CodePilot 不预先分流自由文本。
6. 任务运维统一使用 `codepilot task ...`。
7. 发布统一使用 `codepilot binary ...`。
8. 外部 AI 直接投递任务前必须读取 `codepilot ai template --format json`。
9. 需要计划 artifact 时显式调用 `plan`。

## 推荐命令

### 1. 项目准备

```bash
codepilot setup . --dry-run --json
codepilot setup .
codepilot doctor --project <项目名> --services --json
```

### 2. 提交需求

```bash
codepilot "修复任务重试逻辑并补测试"
codepilot go "修复任务重试逻辑并补测试" -p <项目名>
```

### 3. 计划

```bash
codepilot plan -p <项目名> "明确需求" --json
codepilot plan -p <项目名> --from-spec .codepilot/specs/example.md --json
codepilot inspect -p <项目名> --once --dry-run --write-workflow --json
codepilot workflow status -p <项目名> --json
codepilot workflow next -p <项目名> --list --json
codepilot workflow next -p <项目名> --action <id> --json
codepilot workflow next -p <项目名> --auto --json
```

`plan` 和 `inspect --write-workflow` 不创建 backlog、不启动执行器。

`plan` 的 `--json` 输出包含 `next_actions` 字段，列出后续可用操作（导入任务、重新规划、放弃等）。每个 next action 包含 `id`、`label`、`risk` 和 `suggested_command`。外部 AI / Agent 应优先用 `workflow next --list` 查看可用动作，再用 `workflow next --action <id>` 通过固定 allowlist 安全推进；也可以用 `workflow next --auto` 让 CodePilot 自动选择低风险策略动作。`suggested_command` 只用于展示/审查，不作为自动执行源。默认 `--auto` 不创建 inspect 任务、不导入 plan 任务；项目可通过 `[automation] workflow_auto_create_inspect_tasks`、`workflow_auto_import_plan_tasks`、`workflow_auto_max_steps` 和 `workflow_auto_failure_threshold` 放开策略。高风险动作仍需显式确认，且必须在 `workflow next` allowlist 内。

### 4. 状态与证据

```bash
codepilot status -p <项目名> --json
codepilot hud -p <项目名> --preset full --json
codepilot explore -p <项目名> --prompt "find task template" --json
codepilot trace -p <项目名> --limit 30 --json
codepilot wiki query -p <项目名> "构建" --json
codepilot note show -p <项目名> --json
codepilot memory events -p <项目名> --json
```

`explore` 是只读入口，不写文件、不改 Git、不启动服务、不安装依赖、不执行测试。

`memory events` 读取 `.codepilot/memory/events.jsonl` 中的自动观察事实。CodePilot 会自动生成去重候选并维护 `.codepilot/memory/autocapture.md`；候选会记录 `score`、`feedback` 和 `seen_count`，由 workflow action 和任务终态自动升权/降权，但不会直接写人工维护的长期 wiki/note。

### 5. 任务查看与控制

```bash
codepilot task show <task_id> --json
codepilot task logs <task_id> --tail 80
codepilot task find <关键词> -p <项目名> --json
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task rm <task_id>
```

### 6. 执行与修复闭环

```bash
codepilot run -p <项目名> --once
codepilot daemon -p <项目名> --status
codepilot inspect -p <项目名> --once --json
codepilot inspect -p <项目名> --once --dry-run --write-workflow --json
codepilot build-fix -p <项目名> --task-id <task_id> --dry-run
codepilot build-fix -p <项目名> --task-id <task_id> --json
```

普通 `inspect --dry-run` 保持只输出预览；加 `--write-workflow` 才会写 `.codepilot/context/` 和 Agent Session，并通过 `workflow next` 暴露安全动作。

任务失败后，如需完整修复闭环优先用 `build-fix`；只需重新排队时用 `task retry`。

### 7. Web UI、飞书与 Webhook

```bash
codepilot ui start
codepilot ui status
codepilot ui logs --tail 100
codepilot feishu start
codepilot feishu status
codepilot feishu logs --tail 100
codepilot webhook --host 127.0.0.1 --port 8765
```

飞书自由文本进入当前项目的 OpenCode 会话；没有当前项目时会先返回项目选择卡片。明确命令仍可直接使用：

```text
当前项目状态怎么样
优化飞书任务面板
修复失败任务前先列出候选和风险
tasks failed
retry 123
```

`chat` 启动 CodePilot 管理的 OpenCode TUI。运行时配置、会话数据库和模型选择写入用户级 `~/.codepilot/opencode/<项目标识>/`，与用户自己直接运行的 OpenCode 隔离。

### 8. 事件、Hook、Provider 与 Skill

```bash
codepilot event schema --json
codepilot event list -p <项目名> --json
codepilot hook validate -p <项目名> --json
codepilot exec -p <项目名> --provider codex --dry-run --json -- codex --version
codepilot skill list -p <项目名> --json
codepilot skill run ralplan -p <项目名> --provider codex --input "新增 wiki context" --json
```

### 9. 发布

```bash
codepilot binary prepare --version 0.7.5
codepilot binary release --build-current
codepilot binary verify
```

## 结构化接口

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
```

## 任务模板

如果外部 AI 不走 CodePilot 规划器，而是自己规划后通过 `add -f tasks.json` / `add -f tasks.md` 投递，必须先读取模板：

```bash
codepilot ai template
codepilot ai template --format json
codepilot ai template --format guide
```

强制规则：

1. 人工不要直接 `add`，应走 `codepilot "需求文本"`。
2. `tasks.json` 每条必须带模板合规 `content`。
3. `tasks.md` 每段必须是完整 task-template。
4. `add -t "标题"` 和 `tasks.txt` 会调用 AI 生成 content 并校验。
5. `--no-ai` / `--allow-empty` 已废弃，没有空 content 占位通道。

## 兼容性

这些旧入口已移除：

- `codepilot release ...`
- 顶层 `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- `codepilot chat --no-ui`
- `--no-ai` / `--allow-empty`

统一改用：

- `codepilot binary ...`
- `codepilot task ...`
- `codepilot ui <start|status|logs|stop|restart>`
