# CodePilot AI 调用手册

这份手册是写给其他 AI / Agent 的静态入口。最新机器可读清单以 `codepilot ai manifest` 为准，最新 Markdown 手册以 `codepilot ai guide` 为准。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时优先使用 `--json` 或 `codepilot ai manifest`。
3. 提交高层需求时直接调用 `codepilot "需求文本"` 或 `codepilot go "需求文本"`。
4. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态这类问题应作为问答处理。
5. 在 `chat`、Web UI 会话和飞书自由文本中，创建工作必须显式输入 `# <需求>` / `需求 <内容>` 或 `! <任务>` / `任务 <内容>`。
6. 任务运维统一使用 `codepilot task ...`。
7. 发布统一使用 `codepilot binary ...`。
8. 外部 AI 直接投递任务前必须读取 `codepilot ai template --format json`。

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

### 3. 澄清和计划

```bash
codepilot clarify -p <项目名> "模糊需求" --json
codepilot plan -p <项目名> "明确需求" --json
codepilot plan -p <项目名> --from-spec .codepilot/specs/example.md --json
```

`clarify` 和 `plan` 不创建 backlog、不启动执行器。

### 4. 状态与证据

```bash
codepilot status -p <项目名> --json
codepilot hud -p <项目名> --preset full --json
codepilot explore -p <项目名> --prompt "find task template" --json
codepilot trace -p <项目名> --limit 30 --json
codepilot wiki query -p <项目名> "构建" --json
codepilot note show -p <项目名> --json
```

`explore` 是只读入口，不写文件、不改 Git、不启动服务、不安装依赖、不执行测试。

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
codepilot build-fix -p <项目名> --task-id <task_id> --dry-run
codepilot build-fix -p <项目名> --task-id <task_id> --json
```

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

飞书自由文本建议：

```text
? 当前项目状态怎么样
# 优化飞书任务面板
! 修复一个明确的小问题
```

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
codepilot binary prepare --version 0.1.1
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
- `--no-ai` / `--allow-empty`

统一改用：

- `codepilot binary ...`
- `codepilot task ...`
- `codepilot ui <start|status|logs|stop|restart>`
