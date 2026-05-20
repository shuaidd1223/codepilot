# CodePilot

Language: 中文 | [English](README.en-US.md)

CodePilot 是一个本地工程工作流 CLI，用来把自然语言需求转成可执行任务，并串起规划、执行、审查、巡检、服务运维和发布流程。

当前版本：`0.7.4`

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

`chat`、Web UI 会话和飞书自由文本统一进入 OpenCode + CodePilot MCP。可以像使用 OpenCode 一样直接输入问题、需求或操作意图；需要严格产出规格或计划 artifact 时，再显式调用 `clarify` / `plan` / Web UI 面板入口。

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

### 配置管理

```bash
# 交互式初始化配置文件
codepilot config init
codepilot config init --global
codepilot config init --path /path/to/project

# 验证配置文件
codepilot config validate
codepilot config validate --global
codepilot config validate --fix

# 同步配置文件（补默认项、移除未知项）
codepilot config sync
codepilot config sync --global
codepilot config sync --dry-run
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
prompt = "Task {{ task_id }} failed with {{ error_message }}. Suggest the smallest repair."
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
codepilot binary prepare --version <目标版本号>
codepilot binary release --build-current
codepilot binary verify
codepilot binary where
```

## CLI Agent 家族与兜底链

CodePilot 把可调用的 CLI agent 抽成了 family 注册表，目前内置三家：`claude`、`codex`、`opencode`。
当前面项缺失（未安装/未配置 key/超时）时，按 `[automation] fallback_cli_order` 顺序退到下一家，
默认是 `["claude", "codex", "opencode"]`。OpenCode 作为可更新交互内核，由 CodePilot 在启动时按项目配置注入 provider、模型、MCP 和权限。

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
# Agent-facing task templates, prompts, MCP/OpenCode instructions, and preferred model output language.
# Valid values: en, zh-CN. Default: en.
agent_language = "en"
# Builtin executor dirty-worktree preflight policy.
# stop = skip execution; commit = save a preflight commit; stash = git stash and record restore notes.
preflight_dirty_worktree = "stop"
# 文本模式 CLI 兜底顺序；前面项不可用时按顺序退到下一个
fallback_cli_order = ["claude", "codex", "opencode"]

# OpenCode provider 由 [providers.<name>] 动态生成。
# openai / deepseek / qwen / kimi / 其他 OpenAI 兼容服务都应各自配置独立 provider。
[providers.deepseek]
enabled = true
api_key = ""
base_url = "https://api.deepseek.com"
complex_model = "deepseek-v4-pro"

[providers.openai]
enabled = true
api_key = ""
base_url = "https://api.openai.com/v1"
model = "gpt-5.4"
```

迁移提示：旧版 `[agents] codex_cmd / claude_cmd` 已删除，加载时会抛 `ConfigError` 并给出
迁移示例。运行 `codepilot config sync -p <项目名>` 可一键重写旧文件到新格式。

### OpenCode 专用模式

`codepilot chat -a opencode` 使用官方 OpenCode 二进制作为可更新内核，同时由 CodePilot 在启动前生成
用户级运行时配置：`~/.codepilot/opencode/<项目标识>/opencode.json`、`tui.json` 和 `config/` 目录，并注入：

- `OPENCODE_CONFIG`：包含 CodePilot MCP、默认 agent、commands、instructions、permission、tools。
- `OPENCODE_TUI_CONFIG`：包含 TUI theme、滚动、diff、mouse 配置和 CodePilot 品牌 TUI plugin。
- `OPENCODE_CONFIG_DIR`：包含 `agents/codepilot.md`、`commands/*.md`、`instructions/*.md`、`tui-plugins/codepilot-brand.tsx`，用于 OpenCode 原生 agent/command/plugin 发现。
- `OPENCODE_DISABLE_TERMINAL_TITLE=1`：禁用 OpenCode 自己的终端标题更新，由 CodePilot 把终端窗口/标签标题设置为工具品牌。

OpenCode 套壳品牌、TUI、交互语言规则、默认 agent 和内置 commands 都属于 CodePilot 工具级定制，随包代码发布，不需要业务项目在 `AGENTS.toml` 中配置 `[opencode.*]`。业务项目目录不会生成 `.codepilot/opencode/`；运行时文件只写入用户级 `~/.codepilot/opencode/<项目标识>/`，用来落地项目注册名、MCP 启动命令、隔离会话数据和项目级模型选择。默认注入英文交互规则；如需简体中文 agent 输出，在 `[automation]` 设置 `agent_language = "zh-CN"`。

业务项目只需要配置实际使用的模型供应商和权限策略。例如：

```toml
[opencode.permission]
# 默认 ask；信任当前项目时可改为 full_access；需要细粒度规则时用 custom。
mode = "ask"

[providers.deepseek]
enabled = true
api_key = ""
base_url = "https://api.deepseek.com"
complex_model = "deepseek-v4-pro"
simple_model = "deepseek-v4-flash"
```

用户在 OpenCode TUI 中切换过模型后，CodePilot 会把选择保存为该项目的默认模型。后续新建会话会优先使用项目级选择，不会被 OpenCode 内置免费默认模型覆盖。

这条路径不修改 OpenCode 源码，升级 OpenCode 时继续使用官方 `opencode` 命令即可。当前项目不维护 OpenCode 源码 overlay 或自定义二进制；如果 OpenCode TUI 内部其他硬编码欢迎语、权限弹窗文案仍显示 OpenCode，先记录为官方二进制不可配置边界。

## 已统一的新入口

这些旧入口不要再使用：

- `codepilot release ...`
- 顶层 `codepilot show/logs/stop/retry/find/...`
- `codepilot webui ...`
- `codepilot chat --no-ui` 旧 REPL
- `add --no-ai` / `add --allow-empty`

统一改为：

- 发布：`codepilot binary ...`
- 任务：`codepilot task ...`
- Web UI：`codepilot ui <start|status|logs|stop|restart>`
- Chat：`codepilot chat -a opencode`
- 外部任务投递：先读 `codepilot ai template --format json`，再用模板合规内容调用 `codepilot add ...`

## 文档导航

- 说明文档：[中文](docs/说明文档.zh-CN.md) / [English](docs/说明文档.en-US.md)
- 操作文档：[中文](docs/操作文档.zh-CN.md) / [English](docs/操作文档.en-US.md)
- AI / Agent 调用手册：[中文](docs/AI与Agent调用手册.zh-CN.md) / [English](docs/AI与Agent调用手册.en-US.md)
- Skill 化集成指南：[中文](docs/Skill化集成指南.zh-CN.md) / [English](docs/Skill化集成指南.en-US.md)
- 项目服务说明：[中文](docs/project-services.md) / [English](docs/project-services.en-US.md)
- 工作流状态约定：[中文](docs/workflow-state.zh-CN.md) / [English](docs/workflow-state.en-US.md)
- 静态 AI 调用手册：[中文](AI_USAGE.zh-CN.md) / [English](AI_USAGE.en-US.md)

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
- `AI_USAGE.en-US.md`

## 版本更新日志

版本划分依据来自 Git 历史中的连续大功能批次；早期缺失的历史 tag 已按能力边界回填为语义化版本。`0.x` 阶段允许在 minor 版本中包含入口调整和配置迁移。

### 0.7.4 - 2026-05-20

- Windows：修复 `codepilot ui restart` 后后台 Web UI 子进程弹出黑色控制台窗口的问题。
- 服务启动：Web UI、daemon、inspect、feishu 等后台服务统一追加 `CREATE_NO_WINDOW` 和隐藏启动信息。
- 回归测试：补充冻结二进制后台启动不显示控制台窗口的测试覆盖。

### 0.7.3 - 2026-05-20

- 完整核对二进制业务资源，构建/安装/release 产物会携带飞书 JS 运行时 sidecar。
- 飞书运行时随包包含 `package.json`、`package-lock.json`、`node_modules` 和 `feishu_*.mjs`，避免系统安装后缺 Node SDK。
- 冻结二进制下飞书服务改为从安装目录的 `feishu/` 读取运行时文件，发布包校验也覆盖该目录。

### 0.7.2 - 2026-05-20

- 修复打包后二进制 Web UI 找不到 `index.html` 的问题，静态资源改为优先从包资源读取并保留源码路径兜底。
- PyInstaller 构建显式加入整个 `codepilot/web` 目录，确保 `components`、`boundaries` 等嵌套前端资源随二进制发布。
- 新增 `agent_language` 配置，并补齐 AI manifest / 使用手册的中英文输出支持。

### 0.7.1 - 2026-05-20

- 修复打包后二进制后台启动 Web UI 时误用 `-m codepilot` 导致 `No such option: -m` 的问题。
- 新增 `CODEPILOT_WEBUI_HOST` / `CODEPILOT_WEBUI_PORT` 环境变量，用于覆盖 Web UI 默认监听地址和端口。
- 新增 `CODEPILOT_HOME` 环境变量，用于隔离全局状态、数据库、Web UI/飞书日志和项目运行数据。
- `codepilot-dev` 默认使用 `~/.codepilot-dev` 和 Web UI 端口 `8767`，正式 `codepilot` 继续使用 `~/.codepilot` 的既有数据。

### 0.7.0 - 2026-05-20

- 新增 `config init`、`config validate` 等配置治理入口，并优化配置异常提示。
- 增加 Codex CLI 会话存储与 Windows TUI 兼容支持。
- 强化 MCP 任务模板校验、session 检测和 WebApp 文件搜索性能。
- 继续收敛默认 OpenCode 运行路径，补齐文件上传、`@` 文件引用和配置自动修复能力。
- 统一 EditorConfig / GitAttributes，并补充相关测试。

### 0.6.0 - 2026-05-08 至 2026-05-13

- 引入 CLI family 注册表、`fallback_cli_order`、OpenCode runtime、provider 动态配置和 env bridge。
- 完成 CodePilot MCP server、工具注册表、任务/上下文/运维/外部集成 MCP 工具和 chat 内 MCP 生命周期绑定。
- 新增 scheduled / event agent、成本护栏、循环熔断、审计日志和默认 scheduled agent。
- 扩展二进制构建，支持 vendor CLI 抓取、校验、缓存、bundled CLI 打包与安装释放。
- 更新 OpenCode 工作流隔离配置、运行时 `/agent <name>` 切换和相关 smoke/回归验证。

### 0.5.0 - 2026-04-29 至 2026-04-30

- 新增 `explore`、`wiki`、`note`、`trace`、`hud`、`clarify`、`plan`、`build-fix`、`skill`、`hook` 等项目工作流入口。
- 增强项目级 setup、事件插件、wiki ingest、会话上下文保留和本地技能运行。
- 接入飞书卡片交互、中文命令别名、项目注册、安全确认、需求提交和会话继续能力。
- 完善 DeepSeek provider 配置、任务模板示例、Web UI 草稿隔离和通知呈现。
- 对飞书、AI provider、Web UI、question runtime、agent support 等热点模块做复杂度治理和测试补强。

### 0.4.0 - 2026-04-24 至 2026-04-28

- 收敛命令面板为 `task` / `ui` / `binary` 分组，明确淘汰旧入口。
- 建立结构化 reviewer verdict、Web verdict 面板、progress bus 事件和 LLM heartbeat 渲染。
- 强化任务模板合规校验，删除 `--no-ai` / `--allow-empty` 占位通道，支持 Markdown 批量导入。
- 引入飞书长连接控制、自然语言命令分发、敏感操作二次确认和 secrets 覆盖配置。
- 重构包目录、失败分诊、Web 动作、run_builtin、数据库和分类器等核心热点。

### 0.3.0 - 2026-04-20 至 2026-04-23

- 引入 dual phase agents、worktree 隔离执行、AI triage、项目级 provider 覆盖和全局配置叠加。
- 新增 webhook 服务、项目服务化、daemon 状态反馈、依赖健康/代码规模/复杂度 inspect 信号。
- 拆分 AI Gateway、auto workflow、chat/webui/go 交互控制器、inspect 信号采集和存储层边界。
- 增强 Web UI SSE 可靠性、任务/会话/需求分页、批量操作和任务归档。
- 增加 planner 质量门、任务 evidence 字段、结构化任务模板和防填充任务规则。

### 0.2.0 - 2026-04-16 至 2026-04-19

- 新增 chat mode、Web UI、AI intent routing、daemon 自动启动 UI、WebUI 持久服务和会话管理。
- 增加 doctor、cleanup、inspect 定时扫描、任务去重、需求输入框、任务 cancel/resume/stats 等运维能力。
- 支持 per-task feature branch、进程树清理、Windows 控制台编码修复和 Rich markup 安全转义。
- 引入需求澄清与侦察阶段，拆分 planner/executor，并开始拆分 AI、run、binary 等大模块。

### 0.1.0 - 2026-04-11 至 2026-04-12

- 初始化 CodePilot 项目，建立自然语言工作流入口。
- 接入 Codex 默认工作流、运行时控制、二进制打包和版本化发布流程。
- 提供 AI integration manifest、发布说明和基础审查产物。

## Skill 包

仓库提供可复用 Skill：

- `skills/codepilot-workflow/SKILL.md`

该 Skill 用英文编写，供其他 Codex / Agent 固化 CodePilot 的调用策略、命令顺序和排障流程。安装和维护方式见：[docs/Skill化集成指南.zh-CN.md](docs/Skill化集成指南.zh-CN.md)
