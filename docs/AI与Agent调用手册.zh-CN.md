# CodePilot AI 与 Agent 调用手册

本文面向其他 AI / Agent / 自动化系统，目标是让调用方稳定地读状态、创建明确工作、排障和接入服务。

## 1. 调用原则

1. 优先使用非交互命令；除非需要连续会话，否则避免 `chat`。
2. 需要结构化结果时优先加 `--json`，或直接读取 `codepilot ai manifest`。
3. 提交高层需求时优先使用 `codepilot "需求文本"` 或 `codepilot go "需求文本"`，不要先自行拆任务。
4. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态等输入优先当问答处理。
5. `chat`、Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP；自由文本可以直接表达问题、需求或操作意图。
6. 任务运维统一使用 `codepilot task ...`。
7. 发布统一使用 `codepilot binary ...`。
8. 外部 AI 只有在明确要直接投递已规划任务时才用 `add`，并且必须满足 task-template content 约束。

## 2. 最小命令集合

### 2.1 项目准备

```bash
codepilot setup . --dry-run --json
codepilot setup .
codepilot doctor --project <项目名> --services --json
```

### 2.2 需求、澄清和计划

```bash
codepilot "需求文本"
codepilot go "需求文本" -p <项目名>
codepilot clarify -p <项目名> "模糊需求" --json
codepilot plan -p <项目名> "明确需求" --json
codepilot plan -p <项目名> --from-spec .codepilot/specs/example.md --json
```

`clarify` 和 `plan` 都不创建 backlog、不启动执行器，适合执行前审查。

### 2.3 状态、证据和记忆

```bash
codepilot go "当前项目状态怎么样" -p <项目名>
codepilot status -p <项目名> --json
codepilot hud -p <项目名> --preset full --json
codepilot explore -p <项目名> --prompt "要查询的问题" --json
codepilot trace -p <项目名> --limit 30 --json
codepilot wiki query -p <项目名> "构建" --json
codepilot note show -p <项目名> --json
```

`explore` 是只读取证入口，只返回 `query/evidence/sources/limitations`，不会写文件、改 Git、启动服务、安装依赖或执行测试。

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

### 2.5 执行、后台服务和修复闭环

```bash
codepilot run -p <项目名> --once
codepilot daemon -p <项目名>
codepilot daemon -p <项目名> --status
codepilot daemon -p <项目名> --stop
codepilot inspect -p <项目名> --once --json
codepilot inspect -p <项目名> --status
codepilot build-fix -p <项目名> --task-id <task_id> --json
```

### 2.6 Web UI、飞书和 Webhook

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

飞书自由文本会进入当前项目的 OpenCode 会话。没有当前项目时，机器人会先返回项目选择卡片。明确任务、服务、项目命令仍可直接使用，例如：

```text
当前项目状态怎么样
优化飞书任务面板
tasks failed
retry 123
```

### 2.7 事件、Hook、Provider 和 Skill

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

## 3. 直接投递任务

直接投递任务仅适合外部 AI / 自动化系统已经完成任务规划的场景。

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
- `--no-ai` / `--allow-empty` 已废弃。

## 4. 推荐工作流

### 4.1 提交需求并跟踪

1. `codepilot "需求文本"`
2. `codepilot status -p <项目名> --json`
3. `codepilot task show <task_id> --json`
4. `codepilot task logs <task_id> --tail 80`

### 4.2 先澄清再计划

1. `codepilot clarify -p <项目名> "模糊需求" --json`
2. `codepilot plan -p <项目名> --from-spec <spec_path> --json`
3. 人工确认后再提交需求或通过模板投递任务。

### 4.3 回答项目问题

1. `codepilot go "当前项目状态怎么样" -p <项目名>`
2. 需要确定性数据时读 `status --json`、`hud --json`、`trace --json`。
3. 需要证据时用 `explore --json`、`wiki query --json`。

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
- `--no-ai` / `--allow-empty`

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
