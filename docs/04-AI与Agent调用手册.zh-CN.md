# CodePilot AI 与 Agent 调用手册

语言版本：中文 | [English](04-ai-agent-manual.en-US.md)

本文面向其他 AIAgent自动化系统，目标是让调用方稳定地读状态创建明确工作排障和接入服务。

## 1. 调用原则

1. 优先使用非交互命令；除非需要连续会话，否则避免 `chat`。
2. 需要结构化结果时优先加 `--json`，或直接读取 `codepilot ai manifest`。
3. 提交高层需求时优先使用 `codepilot "需求文本"` 或 `codepilot go "需求文本"`，不要先自行拆任务。
4. 项目状态任务数量完成度失败任务运行中任务服务状态等输入优先当问答处理。
5. `chat`Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP；自由文本可以直接表达问题需求或操作意图。
6. 任务运维统一使用 `codepilot task ...`。
7. 发布统一使用 `codepilot binary ...`。
8. 外部 AI 只有在明确要直接投递已规划任务时才用 `add`，并且必须满足 task-template content 约束。
9. 需要确定性 artifact 时显式调用 `plan` / MCP 工具。

## 2. 最小命令集合

### 2.1 项目准备

```bash
codepilot setup . --dry-run --json
codepilot setup .
codepilot doctor --project <项目名> --services --json
```

### 2.2 需求计划

```bash
codepilot "需求文本"
codepilot go "需求文本" -p <项目名>
codepilot plan -p <项目名> "明确需求" --json
codepilot plan -p <项目名> --from-spec .codepilot/specs/example.md --json
codepilot workflow status -p <项目名> --json
codepilot workflow next -p <项目名> --list --json
codepilot workflow next -p <项目名> --action <id> --json
codepilot workflow next -p <项目名> --auto --json
```

`plan` 不创建 backlog、不启动执行器，适合执行前审查。

`plan` 的文本输入会生成可审查计划 artifact，JSON 输出中的 `plan_path` 指向 `.codepilot/plans/plan-*.md`，`context_path` 指向 `.codepilot/context/plan-*.json`，`task_batch_path` 指向可导入的 tasks JSON。默认只写入 `.codepilot/plans/plan-*.md``.codepilot/context/plan-*.json` 和 `.codepilot/context/plan-*.tasks.json`，不会导入 backlog不会启动执行器。只有人工审查后显式调用 `codepilot workflow next -p <项目名> --action import_tasks --json`，或使用 `codepilot add -p <项目名> -f <task_batch_path>` 导入 task batch，候选任务才会入队。

`plan` 的 `--json` 输出包含 `next_actions` 字段，列出后续可用操作（生成计划、导入任务、放弃等）。外部 AI / Agent 应优先用 `workflow next --list` 查看动作，再用 `workflow next --action <id>` 通过固定 allowlist 安全推进；需要低风险自动推进时可用 `workflow next --auto`。`suggested_command` 只用于展示/审查，不作为自动执行源。

`workflow next --auto` 的默认策略只允许保守低风险动作：忽略已被负反馈降权的巡检报告项，以及基于 inspect context 生成可审查 plan。默认不创建 inspect 任务不导入 plan 任务；如需放开，项目 `AGENTS.toml` 可在 `[automation]` 设置：

```toml
workflow_auto_create_inspect_tasks = false
workflow_auto_import_plan_tasks = false
workflow_auto_max_steps = 1
workflow_auto_failure_threshold = 1
```

### 2.3 状态证据和记忆

```bash
codepilot go "当前项目状态怎么样" -p <项目名>
codepilot status -p <项目名> --json
codepilot hud -p <项目名> --preset full --json
codepilot explore -p <项目名> --prompt "要查询的问题" --json
codepilot trace -p <项目名> --limit 30 --json
codepilot wiki query -p <项目名> "构建" --json
codepilot note show -p <项目名> --json
codepilot memory events -p <项目名> --json
```

`explore` 是只读取证入口，只返回 `query/evidence/sources/limitations`，不会写文件改 Git启动服务安装依赖或执行测试。

`memory events` 读取项目本地自动观察事实日志。CodePilot 会自动生成去重候选并维护 `.codepilot/memory/autocapture.md`；候选会记录 `score``feedback` 和 `seen_count`，由 workflow action 和任务终态自动升权/降权，但它不直接写人工维护的长期 wiki/note。

### 2.4 任务控制

```bash
codepilot task show <task_id> --json
codepilot task logs <task_id> --tail 80
codepilot task find <关键词> -p <项目名> --json
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task edit <task_id> --status backlog
codepilot task rm <task_id>
codepilot task sweep <task_id>
```

### 2.5 执行后台服务和修复闭环

```bash
codepilot run -p <项目名> --once
codepilot daemon -p <项目名>
codepilot daemon -p <项目名> --status
codepilot daemon -p <项目名> --stop
codepilot inspect -p <项目名> --once --json
codepilot inspect -p <项目名> --once --dry-run --write-workflow --json
codepilot inspect -p <项目名> --status
codepilot build-fix -p <项目名> --task-id <task_id> --json
```

`inspect --write-workflow` 只支持 `--once --dry-run`，会把巡检结果写入项目本地 workflow context，并生成 `create_inspect_tasks``promote_inspect_report_<candidate_id>``ignore_inspect_report_<candidate_id>``delete_inspect_report_<candidate_id>``archive_inspect_report_<candidate_id>``plan_from_inspect` 等安全 `next_actions`；不会直接创建 backlog 或启动执行器。

### 2.6 Web UI飞书和 Webhook

```bash
codepilot ui start
codepilot ui status
codepilot ui logs --tail 100
codepilot ui stop
codepilot feishu start
codepilot feishu status
codepilot feishu logs --tail 100
codepilot feishu stop
codepilot webhook --host 127.0.0.1 --port 8765
```

飞书自由文本会进入当前项目的 OpenCode 会话。没有当前项目时，机器人会先返回项目选择卡片。明确任务服务项目命令仍可直接使用，例如：

```text
当前项目状态怎么样
优化飞书任务面板
修复失败任务前先列出候选和风险
tasks failed
retry 123
```

`codepilot chat -a opencode` 启动 CodePilot 管理的 OpenCode TUI。运行时配置会话数据库和项目级模型选择位于用户级 `~/.codepilot/opencode/<项目标识>/`，不读取或覆盖用户自己直接运行 OpenCode 的配置与会话。

### 2.7 事件HookProvider 和 Skill

```bash
codepilot event schema --json
codepilot event list -p <项目名> --json
codepilot event test -p <项目名> --event doctor.checked --json
codepilot hook validate -p <项目名> --json
codepilot hook test -p <项目名> --provider codex --event agent.prompt.submitted --json
codepilot exec -p <项目名> --provider codex --dry-run --json -- codex --version
codepilot skill list -p <项目名> --json
codepilot skill run ralplan -p <项目名> --provider codex --input "需求" --json
```

### 2.8 发布与安装

```bash
codepilot binary build
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <版本号>
codepilot binary where
```

### 2.9 机器可读说明

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
codepilot ai template --format json
codepilot ai template --format guide
```

### 2.10 MCP 默认工具契约

`codepilot mcp serve --list-tools` 和 MCP `list_tools` 使用同一份默认注册表。默认公开工具数为 25 个，不包含运行时健康检查工具 `codepilot.health`。启动真实 MCP 服务时，`codepilot.health` 会额外注册用于探活。

默认公开工具：

- `archive_task`
- `build_fix`
- `create_task`
- `daemon_status`
- `doctor`
- `edit_task`
- `exec`
- `explore`
- `feishu_notify`
- `feishu_send_to_user`
- `generate_breakdown`
- `hook_trigger`
- `inspect_project`
- `inspect_workflow`
- `list_tasks`
- `note_add`
- `run_once`
- `show_task`
- `stop_task`
- `validate_task_template`
- `webhook_invoke`
- `wiki_add`
- `wiki_query`
- `workflow_next`
- `workflow_status`

## 3. 直接投递任务

直接投递任务仅适合外部 AI自动化系统已经完成任务规划的场景。

```bash
codepilot ai template --format json
codepilot add -p <项目名> -t "任务标题"
codepilot add -p <项目名> -f tasks.json
codepilot add -p <项目名> -f tasks.md
codepilot add -p <项目名> -f tasks.txt
```

强制规则：

- 所有 content 都必须符合 task-template 的必填章节。
- `tasks.json` 每条必须带模板合规 `content`。
- `tasks.md` 每个 `---` 分隔段必须是完整 task-template。
- `tasks.txt` 和 `add -t` 会调用 AI 生成 content 并校验。
- `--no-ai``--allow-empty` 已废弃。

## 4. 推荐工作流

### 4.1 提交需求并跟踪

1. `codepilot "需求文本"`
2. `codepilot status -p <项目名> --json`
3. `codepilot task show <task_id> --json`
4. `codepilot task logs <task_id> --tail 80`

### 4.2 先计划再执行

1. `codepilot plan -p <项目名> "需求" --json`
   - 输出包含 `next_actions`，推荐下一步动作。
2. `codepilot workflow next -p <项目名> --list --json`
   - 审查可用动作风险等级和展示用 `suggested_command`。
   - 低风险自动推进可改用 `codepilot workflow next -p <项目名> --auto --json`。
3. `codepilot workflow next -p <项目名> --action import_tasks --json`
   - 通过 allowlist 从 plan 导入任务。
4. `codepilot workflow next -p <项目名> --action import_tasks --json`
   - 人工审查 plan 后，通过 allowlist 导入候选任务。

### 4.6 Artifact 后续动作说明

当 `plan` 生成 artifact 后，其 `--json` 输出和对应的 workflow state 都会记录 `next_actions`，字段结构如下：

```json
{
  "id": "import_tasks",
  "label": "根据当前 plan 导入任务",
  "risk": "low",
  "suggested_command": "codepilot workflow next -p demo --action import_tasks --json"
}
```

| 字段 | 说明 |
| :--- | :--- |
| `id` | 动作标识，用于程序化引用 |
| `label` | 人类可读的说明文字 |
| `risk` | 风险等级：`low``medium``high` |
| `suggested_command` | 展示/审查用命令提示，不能作为自动执行源 |

推荐路径：

- **列出动作**：`codepilot workflow next -p <项目名> --list --json`。
- **低风险自动推进**：`codepilot workflow next -p <项目名> --auto --json` 只选择策略允许的低风险动作。
- **Plan**：用 `codepilot workflow next -p <项目名> --action plan_from_spec --json` 生成执行计划。
- **Plan → Task**：人工审查 plan 后，用 `codepilot workflow next -p <项目名> --action import_tasks --json` 将候选任务导入 backlog。
- **Plan → Execute**：高风险动作默认拒绝；即使显式允许，也必须在 `workflow next` allowlist 内。
- **放弃**：删除 artifact 文件，流程终止。

`next_actions` 默认不执行任何自动化操作，所有后续动作都需要人工确认或显式调用 `workflow next`。`workflow next` 不会 shell 执行 `suggested_command` 字符串。

### 4.3 回答项目问题

1. `codepilot go "当前项目状态怎么样" -p <项目名>`
2. 需要确定性数据时读 `status --json``hud --json``trace --json`。
3. 需要证据时用 `explore --json``wiki query --json`。

### 4.4 失败任务恢复

1. `codepilot task logs <task_id> --full`
2. `codepilot build-fix -p <项目名> --task-id <task_id> --dry-run`
3. `codepilot build-fix -p <项目名> --task-id <task_id> --json`
4. 如只需重新排队，用 `codepilot task retry <task_id>`。

### 4.5 发布前检查

1. `codepilot binary prepare --version <版本号>`
2. `codepilot binary verify`

## 5. JSON 输出契约

成功：

```json
{
  "ok": true,
  "command": "status",
  "data": {}
}
```

失败：

```json
{
  "ok": false,
  "command": "show",
  "data": {"task": null, "logs": []},
  "error": {"message": "任务不存在", "code": "task_not_found"}
}
```

调用方应优先分支处理 `error.code`，不要盲目重试。

## 6. 兼容性说明

以下旧入口已移除：

- `codepilot release ...`
- 顶层 `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- `codepilot chat --no-ui`
- `--no-ai``--allow-empty`

请统一改为：

- `codepilot binary ...`
- `codepilot task ...`
- `codepilot ui <start|status|logs|stop|restart>`

## 7. 结合 Skill 使用

仓库提供 Skill：

- `skills/codepilot-workflow/SKILL.md`

支持 `$skill` 的 Agent 应优先通过该 Skill 固化调用策略和排障流程。Skill 里的详细命令映射见：

- `skills/codepilot-workflow/references/command-map.md`
- `skills/codepilot-workflow/references/agent-playbooks.md`
