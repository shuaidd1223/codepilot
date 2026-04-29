# CodePilot AI 与 Agent 调用手册（命令集合）

本文面向其他 AI / Agent / 自动化系统。

## 1. 调用原则

1. 优先非交互命令，避免 `chat` 模式。
2. 优先结构化输出：`--json`。
3. 提交需求优先使用自然语言入口：`codepilot "需求文本"`。
4. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态等输入优先当问答处理，不要直接创建任务。
5. `chat`、Web UI 会话和飞书自由文本中，疑似需求/任务必须显式前缀确认后才创建工作：`? <问题>`、`# <需求>`、`! <任务>`。
6. 任务运维统一使用 `codepilot task ...`。
7. 发布统一使用 `codepilot binary ...`。
8. **人工不应直接调 `add`**；要新增任务请用 `codepilot "需求文本"` 让规划器拆。`add` 命令主要为外部 AI / 智能体批量投递任务设计。
9. **AI / 智能体调 `add` 必须带 task-template 合规 content**；没有空 content 占位通道，缺章节直接拒。先 `codepilot ai template --format json` 拿 schema。

## 2. 最小命令集合

### 2.1 初始化与需求

```bash
codepilot init .
codepilot "需求文本"
codepilot go "需求文本"
codepilot go "当前项目有多少任务，完成了多少" -p <项目名>
codepilot chat -p <项目名>
```

### 2.2 状态与查询（建议 JSON）

```bash
codepilot status -p <项目名> --json
codepilot explore --prompt "要查询的问题" -p <项目名> --json
codepilot task show <task_id> --json
codepilot task find <关键词> -p <项目名> --json
codepilot doctor --json
```

`explore` 是只读项目探索入口，只返回 `query/evidence/sources/limitations`，用于澄清和计划前取证。它不会写文件、改 Git、启动服务、安装依赖或执行测试；修改类请求会被拒绝并提示改走普通 workflow。

### 2.3 任务控制

```bash
codepilot task logs <task_id> --tail 80
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task resume <task_id>
codepilot task done <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task edit <task_id> --status backlog
codepilot task rm <task_id>
codepilot task sweep <task_id>
```

### 2.4 执行与后台服务

```bash
codepilot run -p <项目名>
codepilot daemon -p <项目名>
codepilot daemon -p <项目名> --status
codepilot daemon -p <项目名> --stop
codepilot inspect -p <项目名> --once
codepilot inspect -p <项目名> --status
codepilot inspect -p <项目名> --stop
codepilot ui start
codepilot ui status
codepilot ui logs --tail 100
```

### 2.5 飞书与 Webhook

```bash
codepilot feishu start
codepilot feishu status
codepilot feishu logs --tail 100
codepilot feishu stop
codepilot webhook --host 127.0.0.1 --port 8765
```

飞书自由文本建议使用符号前缀：

```text
? 当前项目状态怎么样
# 优化飞书任务面板
! 修复一个明确的小问题
```

### 2.6 发布与安装

```bash
codepilot binary build
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <版本号>
codepilot binary where
```

### 2.7 机器可读说明

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
codepilot ai template --format json    # 任务模板字段 schema + 批量导入格式
codepilot ai template --format guide   # 中文填充指南
```

### 2.8 直接投递任务（仅 AI / 智能体）

预先按模板规划好任务后，三种批量格式：

```bash
# 单条：让 codepilot 用 --agent 指定的 AI 生成模板合规 content（推荐）
codepilot add -p <项目名> -t "任务标题"

# 批量 JSON：每条带 content
codepilot add -p <项目名> -f tasks.json

# 批量 Markdown：多个完整 task-template，用 `---` 分隔
codepilot add -p <项目名> -f tasks.md

# 批量纯文本：每行一个标题，逐条 AI 生成
codepilot add -p <项目名> -f tasks.txt
```

**强制规则**：
- 所有路径都做模板合规校验（必填 9 个章节）；缺章节立即整批拒绝。
- 没有 `--no-ai` / `--allow-empty` 占位通道。
- 章节骨架英文（`## Task Goal` / `## In Scope` ...），章节正文中文。

## 3. JSON 输出契约

```json
{
  "ok": true,
  "command": "status",
  "data": {}
}
```

错误示例：

```json
{
  "ok": false,
  "command": "show",
  "data": {"task": null, "logs": []},
  "error": {"message": "任务不存在", "code": "task_not_found"}
}
```

## 4. 推荐工作流模板

### 4.1 提交需求并跟踪

1. `codepilot "需求文本"`
2. `codepilot status -p <项目名> --json`
3. `codepilot task show <task_id> --json`
4. `codepilot task logs <task_id> --tail 80`

### 4.2 问答与确认式执行

1. 问状态：`codepilot go "当前项目状态怎么样" -p <项目名>`
2. 进 `chat` 或飞书时，普通问题直接问或加 `?`。
3. 要创建需求时，明确使用 `# <需求内容>`。
4. 要创建单步任务时，明确使用 `! <任务内容>`。
5. 收到“不会直接执行”的确认提示后，只有用户确认要创建工作时才重发 `#` 或 `!`。

### 4.3 失败任务恢复

1. `codepilot task logs <task_id> --full`
2. 修复环境或代码上下文
3. `codepilot task retry <task_id>`
4. `codepilot run -p <项目名>`

### 4.4 发布前检查

1. `codepilot binary prepare --version <版本号>`
2. `codepilot binary verify`

### 4.5 飞书/通知排障

1. `codepilot feishu status`
2. `codepilot feishu logs --tail 100`
3. 检查 `AGENTS.toml` 的 `[feishu_bot]` 和 `.codepilot.secrets.toml`。
4. Webhook 飞书通知失败时，检查 `[notifications]` 的 `provider = "feishu"`、`webhook_url` 和 `webhook_secret`。

## 5. 兼容性说明

以下旧入口已移除：

- `codepilot release ...`
- 顶层 `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- `--no-ai` / `--allow-empty` 空任务占位通道

请统一改为：

- `codepilot binary ...`
- `codepilot task ...`
- `codepilot ui <start|status|logs|stop|restart>`

## 6. 结合 Skill 使用

仓库已提供 Skill：

- `skills/codepilot-workflow/SKILL.md`

若你的 Agent 支持 `$skill` 机制，优先通过该 Skill 固化调用策略和命令顺序。
