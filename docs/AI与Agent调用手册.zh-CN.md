# CodePilot AI 与 Agent 调用手册（命令集合）

本文面向其他 AI / Agent / 自动化系统。

## 1. 调用原则

1. 优先非交互命令，避免 `chat` 模式。
2. 优先结构化输出：`--json`。
3. 提交需求优先使用自然语言入口：`codepilot "需求文本"`。
4. 任务运维统一使用 `codepilot task ...`。
5. 发布统一使用 `codepilot binary ...`。

## 2. 最小命令集合

### 2.1 初始化与需求

```bash
codepilot init .
codepilot "需求文本"
codepilot go "需求文本"
```

### 2.2 状态与查询（建议 JSON）

```bash
codepilot status -p <项目名> --json
codepilot task show <task_id> --json
codepilot task find <关键词> -p <项目名> --json
codepilot doctor --json
```

### 2.3 任务控制

```bash
codepilot task logs <task_id> --tail 80
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task edit <task_id> --status backlog
```

### 2.4 执行与后台服务

```bash
codepilot run -p <项目名>
codepilot daemon -p <项目名>
codepilot daemon -p <项目名> --status
codepilot daemon -p <项目名> --stop
codepilot ui start
codepilot ui status
codepilot ui logs --tail 100
```

### 2.5 发布与安装

```bash
codepilot binary build
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <版本号>
```

### 2.6 机器可读说明

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
```

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

### 4.2 失败任务恢复

1. `codepilot task logs <task_id> --full`
2. 修复环境或代码上下文
3. `codepilot task retry <task_id>`
4. `codepilot run -p <项目名>`

### 4.3 发布前检查

1. `codepilot binary prepare --version <版本号>`
2. `codepilot binary verify`

## 5. 兼容性说明

以下旧入口已移除：

- `codepilot release ...`
- 顶层 `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`

请统一改为：

- `codepilot binary ...`
- `codepilot task ...`
- `codepilot ui <start|status|logs|stop|restart>`

## 6. 结合 Skill 使用

仓库已提供 Skill：

- `skills/codepilot-workflow/SKILL.md`

若你的 Agent 支持 `$skill` 机制，优先通过该 Skill 固化调用策略和命令顺序。
