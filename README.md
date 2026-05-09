# CodePilot

CodePilot 是一个本地工程工作流 CLI，用来把自然语言需求转成可执行任务，并串起规划、执行、审查、巡检、服务运维和发布流程。

## 快速开始

首次接入一个仓库：

```bash
codepilot setup .
codepilot doctor --project <项目名> --services
```

提交明确需求，让 CodePilot 判断是否拆分并按配置执行：

```bash
codepilot "实现自动拆分和自动执行工作流"
codepilot go "修复任务重试逻辑并补测试" -p <项目名>
```

询问项目状态、任务统计或服务状态：

```bash
codepilot go "当前项目有多少任务，完成了多少" -p <项目名>
codepilot status -p <项目名> -v
codepilot hud -p <项目名> --preset full
```

`chat`、Web UI 会话和飞书自由文本采用确认式执行语义。普通问题可以直接问，也可以用 `? <问题>`；创建需求必须用 `# <需求>` 或 `需求 <内容>`；创建单步任务必须用 `! <任务>` 或 `任务 <内容>`。

## 常用命令

### 项目与需求

```bash
codepilot init .
codepilot setup . --dry-run --json
codepilot clarify -p <项目名> "模糊需求" --json
codepilot plan -p <项目名> "明确需求" --json
codepilot auto -p <项目名> -t "高层目标" --plan-only
```

### 只读上下文与记忆

```bash
codepilot explore -p <项目名> --prompt "要查询的问题" --json
codepilot wiki query -p <项目名> "构建" --json
codepilot note add -p <项目名> "当前验证命令是 pytest tests"
codepilot trace -p <项目名> --limit 30
```

### 任务运维

```bash
codepilot task show <task_id>
codepilot task logs <task_id> --tail 80
codepilot task find <关键词> -p <项目名>
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task rm <task_id>
```

### 队列、服务与排障

```bash
codepilot run -p <项目名> --once
codepilot daemon -p <项目名>
codepilot daemon -p <项目名> --status
codepilot inspect -p <项目名> --once
codepilot inspect -p <项目名> --status
codepilot build-fix -p <项目名> --task-id <task_id> --json
codepilot doctor --project <项目名> --services --json
```

### Scheduled / Event Agent

`scheduled` 子命令用于查看、手动运行或禁用 `AGENTS.toml` 中声明的定时 agent。
事件触发 agent 由事件流构造任务，不通过 `scheduled run-once` 直接触发。

```bash
codepilot scheduled list -p <项目名>
codepilot scheduled show task_health -p <项目名> --json
codepilot scheduled run-once task_health --dry-run
codepilot scheduled run-once task_health -p <项目名> --dry-run
codepilot scheduled disable task_health -p <项目名>
```

最小配置示例：

```toml
[automation.scheduled_agents.task_health]
enabled = true
agent = "codex"
interval = "10m" # 也可使用 schedule = "0 9 * * *"
prompt = "Review local CodePilot task status and summarize risks."
max_cost_usd = 0.10
max_daily_cost_usd = 0.50

[automation.event_agents.failed_task_triage]
enabled = false
trigger = "task.failed"
agent = "codex"
prompt = "Task {{ task.id }} failed with {{ task.error }}. Suggest the smallest repair."
max_cost_usd = 0.10
max_daily_cost_usd = 0.50
```

本仓库默认提供三个 scheduled agent：`task_health` 检查任务健康，`daily_summary`
准备日报摘要，`auto_inspect` 生成轻量巡检建议。运行记录写入
`.codepilot/scheduled/audit.jsonl`，成本和循环护栏状态写入
`.codepilot/scheduled/guards.json`。

### Web UI、飞书、Webhook

```bash
codepilot ui
codepilot ui start
codepilot ui logs --tail 100
codepilot feishu start
codepilot feishu status
codepilot webhook --host 127.0.0.1 --port 8765
```

### AI、事件、Hook、Provider 与 Skill

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai template --format json
codepilot event schema --json
codepilot hook validate -p <项目名> --json
codepilot exec -p <项目名> --provider codex --dry-run --json -- codex --version
codepilot skill list -p <项目名> --json
```

### 二进制构建与发布

```bash
codepilot binary build
codepilot binary install --binary <path-to-binary>
codepilot binary prepare --version 0.1.1
codepilot binary release --build-current
codepilot binary verify
codepilot binary where
```

## CLI Agent 家族与兜底链

CodePilot 把可调用的 CLI agent 抽成了 family 注册表，目前内置三家：`claude`、`codex`、`opencode`。
当前面项缺失（未安装/未配置 key/超时）时，按 `[automation] fallback_cli_order` 顺序退到下一家，
默认是 `["claude", "codex", "opencode"]`——OpenCode 作为最终兜底，靠**已配置的任一 LLM API key 即可工作**。

`AGENTS.toml` 中的相关字段：

```toml
[agents]
# planner/builder/reviewer 留空时使用对应场景默认值
planner = ""
builder = ""
reviewer = ""

[agents.commands]
# CLI family -> 命令名/绝对路径；缺失项默认使用同名命令
claude = "claude"
codex = "codex"
opencode = "opencode"

[automation]
# 文本模式 CLI 兜底顺序；前面项不可用时按顺序退到下一个
fallback_cli_order = ["claude", "codex", "opencode"]

# OpenCode 是 provider-agnostic 的；启动时按以下顺序自动挑选可用 backend：
#   1. anthropic（来自 claude-opus / claude-sonnet / claude-haiku 的 api_key）
#   2. deepseek（OpenAI 兼容 + base_url）
#   3. openai（openai-gpt4* 的 api_key）
[providers.opencode]
enabled = true
```

迁移提示：旧版 `[agents] codex_cmd / claude_cmd` 已删除，加载时会抛 `ConfigError` 并给出
迁移示例。运行 `codepilot config sync -p <项目名>` 可一键重写旧文件到新格式。

## 已统一的新入口

这些旧入口不要再使用：

- `codepilot release ...`
- 顶层 `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- `add --no-ai` / `add --allow-empty`

统一改为：

- 发布：`codepilot binary ...`
- 任务：`codepilot task ...`
- Web UI：`codepilot ui <start|status|logs|stop|restart>`
- 外部任务投递：先读 `codepilot ai template --format json`，再用模板合规内容调用 `codepilot add ...`

## 文档导航

- 说明文档：[docs/说明文档.zh-CN.md](docs/说明文档.zh-CN.md)
- 操作文档：[docs/操作文档.zh-CN.md](docs/操作文档.zh-CN.md)
- AI / Agent 调用手册：[docs/AI与Agent调用手册.zh-CN.md](docs/AI与Agent调用手册.zh-CN.md)
- Skill 化集成指南：[docs/Skill化集成指南.zh-CN.md](docs/Skill化集成指南.zh-CN.md)
- 项目服务说明：[docs/project-services.md](docs/project-services.md)

## 给其他 AI / Agent 的标准入口

机器可读命令清单：

```bash
codepilot ai manifest
```

AI 调用手册：

```bash
codepilot ai guide
```

短提示词：

```bash
codepilot ai prompt
```

仓库根目录还保留静态产物：

- `AI_MANIFEST.json`
- `AI_USAGE.zh-CN.md`

## Skill 包

仓库提供可复用 Skill：

- `skills/codepilot-workflow/SKILL.md`

该 Skill 用英文编写，供其他 Codex / Agent 固化 CodePilot 的调用策略、命令顺序和排障流程。安装和维护方式见：[docs/Skill化集成指南.zh-CN.md](docs/Skill化集成指南.zh-CN.md)
