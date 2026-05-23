# CodePilot

<p align="center">
  <img src="docs/codepilot-logo.png" alt="CodePilot Logo" width="180">
</p>

<p align="center">
  <strong>本地工程工作流 CLI — 自然语言驱动开发全流程</strong>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="CodePilot Demo" width="720">
</p>

<p align="center">
  <a href="https://gitee.com/shuai_dd/CodePilot"><img src="https://img.shields.io/badge/Gitee-仓库-red" alt="Gitee"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License"></a>
  <a href="#"><img src="https://img.shields.io/badge/version-0.7.4-green" alt="version"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey" alt="platform"></a>
  <a href="#"><img src="https://img.shields.io/badge/macOS-未测试-orange" alt="macOS"></a>
</p>

> **平台支持**：仅在 Windows 和 Linux 上测试通过。macOS 未经过充分测试，实际使用可能存在较多 bug。欢迎社区贡献 macOS 适配。

Language: 中文 | [English](README.en-US.md)

CodePilot 是一个本地工程工作流 CLI，用来把自然语言需求转成可执行任务，并串起规划、执行、审查、巡检、服务运维和发布流程。

当前版本：`0.7.4` | 作者：[帅呆呆](https://gitee.com/shuai_dd) | Gitee：[shuai_dd/CodePilot](https://gitee.com/shuai_dd/CodePilot)

## 演示

### 录屏演示

<!-- 录制方法：使用 ScreenToGif 或 LICEcap 录制终端操作，导出为 docs/demo.gif -->

![demo](docs/demo.gif)

*演示：从自然语言需求到自动执行完成的完整工作流。*

### 截图展示

<p align="center">
  <img src="docs/screenshot-webui.png" alt="Web UI" width="400">
  <img src="docs/screenshot-task.png" alt="Task Management" width="400">
</p>

*左：Web UI 管理面板 | 右：任务状态与运维面板*

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

## 典型工作流

### 需求 → 任务 → 执行

```bash
# 1. 澄清模糊需求
codepilot clarify -p myproject "用户登录支持手机验证码" --json

# 2. 生成执行计划
codepilot plan -p myproject "实现手机验证码登录" --json

# 3. 自动执行（含代码编写 + 测试 + 审查）
codepilot auto -p myproject -t "实现手机验证码登录"

# 4. 查看执行结果
codepilot task show <task_id>
codepilot task logs <task_id> --tail 50
```

### 代码审查与修复

```bash
# 提交代码审查
codepilot "审查最近的提交" -p myproject

# 自动修复构建错误
codepilot build-fix -p myproject --task-id <task_id> --json
```

### 项目巡检

```bash
# 启动后台巡检服务
codepilot inspect -p myproject

# 查看巡检状态
codepilot inspect -p myproject --status

# 单次全量检查
codepilot inspect -p myproject --once
codepilot inspect -p myproject --once --dry-run --write-workflow --json
codepilot doctor --project myproject --services --json
```

`--write-workflow` 会生成巡检工作流上下文和安全 `next_actions`，可继续用 `codepilot workflow next -p myproject --list --json` 审查，或用 `codepilot workflow next -p myproject --auto --json` 执行低风险自动推进动作。`--auto` 永远不执行 `suggested_command` 字符串；默认只做保守动作，不自动创建 inspect 任务、不自动导入 plan 任务。

可在项目 `AGENTS.toml` 的 `[automation]` 中放开更激进的自动推进：

```toml
workflow_auto_create_inspect_tasks = false
workflow_auto_import_plan_tasks = false
workflow_auto_max_steps = 1
workflow_auto_failure_threshold = 1
```

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
codepilot memory events -p <项目名> --json
codepilot trace -p <项目名> --limit 30
```

`memory events` 会读取项目本地自动观察日志，并把 workflow action、任务成功/失败等反馈沉淀为带 `score` / `feedback` / `seen_count` 的去重候选。

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
codepilot inspect -p <项目名> --once --dry-run --write-workflow --json
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

## 项目架构

### 业务场景

CodePilot 覆盖本地工程工作流的全生命周期，服务于 7 个核心场景：

| 场景 | 说明 | 核心入口 |
| --- | --- | --- |
| **需求→任务** | 模糊需求澄清为结构化 spec，拆分为可执行任务，自动规划并逐任务执行 | `codepilot go "..."`, `codepilot clarify`, `codepilot plan` |
| **代码审查闭环** | 双阶段 agent 协作 — builder 实现、reviewer 审查，FAIL 则回到 builder 修复直至通过或达到最大轮数 | `codepilot auto`, `codepilot build-fix` |
| **项目巡检** | 多维信号采集：代码规模与复杂度、依赖健康、TODO 标记、失败任务、git 活跃度；产出可审查的候选报告并支持写入工作流上下文 | `codepilot inspect --once`, `codepilot doctor` |
| **多通道交互** | CLI 文本模式、Web UI 管理面板、飞书/Lark 机器人双向交互、Webhook HTTP 回调 | `codepilot ui start`, `codepilot feishu start`, `codepilot webhook` |
| **自治 Agent** | Cron 定时或事件触发 (task.failed 等) 的 agent 作业，带单次/每日成本护栏、循环熔断和审计日志 | `codepilot scheduled list`, `codepilot scheduled run-once` |
| **二进制发布** | PyInstaller 冻结二进制一键构建，vendor CLI (codex/opencode) 自动抓取、校验、缓存与打包 | `codepilot binary build`, `codepilot binary release` |
| **多 AI 运行时** | Claude Code / Codex CLI / OpenCode 三家 CLI agent 族注册，支持兜底链自动切换；OpenCode 作为可更新 TUI 交互内核 | `codepilot chat -a opencode`, `codepilot exec` |

### 模块地图

```
codepilot/
├── cli.py                     # CLI 入口, 自然语言路由, 50+ 命令懒加载注册
├── commands/                  # Click 子命令 (按领域拆分, 共 ~70 个文件)
│   ├── auto.py                #   `auto`/`go` 入口：需求→任务主流程
│   ├── auto_chat.py           #   chat 模式交互管线 (意图分发、会话管理)
│   ├── auto_chat_commands.py  #   chat 内斜杠命令 (/task, /inspect, /plan ...)
│   ├── auto_workflow.py       #   自然语言工作流：项目解析→意图→规划→执行
│   ├── auto_workflow_planning.py # 规划器调度 (两阶段侦察 + 拆分)
│   ├── auto_project_resolution.py # 项目解析 (按名称/路径/CWD 自动发现)
│   ├── clarify.py             #   需求澄清流程 (模糊需求→结构化 spec)
│   ├── plan.py                #   执行计划生成 (spec→任务拆分→验收标准)
│   ├── run.py                 #   任务执行入口 (CLI 命令 + 共享 helper)
│   ├── run_orchestrator.py    #   队列编排 (上下文解析、workspace 准备、executor 分发)
│   ├── run_builtin.py         #   内置执行器 CLI 入口
│   ├── run_builtin_core.py    #   内置执行器共享工具 (agent 解析、preflight、runtime)
│   ├── run_builtin_executor.py #  内置双阶段执行引擎 (builder + reviewer 闭环)
│   ├── run_builtin_prompts.py #   内置执行器 prompt 构造
│   ├── run_live_runner.py     #   实时子进程执行监控 (心跳、输出过滤、取消)
│   ├── run_git.py             #   Git 隔离操作 (branch/worktree 创建、preflight 检查)
│   ├── run_shell.py           #   跨平台 shell 命令执行与检测
│   ├── run_failure_triage.py  #   失败分诊主模块 (重导出)
│   ├── run_failure_triage_apply.py    # 分诊决策执行 (重试/replan/discard)
│   ├── run_failure_triage_decisions.py # 分诊决策引擎 (证据收集、决策映射)
│   ├── run_failure_triage_prompts.py  # 分诊 prompt 构造
│   ├── reviewer_output.py     #   Reviewer 输出解析 (PASS/FAIL verdict)
│   ├── inspect.py             #   巡检 CLI + 信号采集调度
│   ├── inspect_service.py     #   巡检后台服务生命周期
│   ├── inspect_lifecycle.py   #   巡检报告生命周期 (promote/ignore/delete/archive)
│   ├── inspect_signals.py     #   信号指纹与分组
│   ├── inspect_signal_collectors*.py # 信号采集器 (代码量/依赖健康/TODO)
│   ├── inspect_workflow.py    #   巡检→工作流上下文写入
│   ├── task.py                #   `task` 命令组入口
│   ├── tasks.py               #   任务 CRUD 命令实现
│   ├── task_quality.py        #   任务质量校验
│   ├── chat.py                #   `chat` 交互会话 (OpenCode 内核)
│   ├── config_cmd.py          #   `config init/validate/sync` 配置治理
│   ├── binary.py              #   `binary build/install/release/verify`
│   ├── doctor.py              #   项目全面健康诊断
│   ├── daemon.py              #   任务队列守护进程
│   ├── scheduled.py           #   定时/事件 agent 管理
│   ├── feishu.py              #   飞书服务控制
│   ├── webui_service.py       #   Web UI 后台服务 (start/stop/restart/logs)
│   ├── hud.py                 #   项目仪表盘 (任务统计/健康/趋势)
│   ├── status.py              #   项目状态展示
│   ├── explore.py             #   只读项目探索 (代码搜索/问答)
│   ├── wiki.py                #   项目知识库 (ingest/query)
│   ├── note.py                #   项目笔记管理
│   ├── trace.py               #   审计追溯 (操作历史)
│   ├── memory.py              #   memory events CLI
│   ├── setup.py               #   项目初始化向导
│   ├── init.py                #   快速初始化
│   ├── cleanup.py             #   过期数据清理
│   ├── hook.py, event.py      #   Hook 与事件管理
│   ├── exec_cmd.py            #   透传执行外部 CLI agent
│   ├── skill.py               #   Skill 包管理
│   ├── add.py                 #   外部任务投递入口
│   ├── self_update.py         #   工具自更新
│   ├── mcp.py                 #   MCP 调试命令
│   ├── requirement_worker.py  #   需求 Worker 入口
│   └── ...
├── core/                      # 核心基础设施
│   ├── config.py              #   AGENTS.toml 发现、解析、完整数据模型 (AgentsConfig 等 10+ 配置类)
│   ├── config_builder.py      #   配置构造器 (from_dict、secrets overlay、provider 合并)
│   ├── config_parse.py        #   配置解析校验 (agent 规范化、值域检查)
│   ├── workflow_state.py      #   工作流状态机 (mode state/session/artifact 的 JSON 文件持久化)
│   ├── runtime.py             #   进程管理 (启动/心跳/终止)、subprocess 工具、Windows console 抑制
│   ├── memory.py              #   观察记忆系统 (事件追加→去重合并→评分→候选沉淀→autocapture.md)
│   ├── event_plugins.py       #   事件插件总线 (发布/订阅/生命周期)
│   ├── hook_registry.py       #   Hook 注册与执行 (pre/post task, pre/post phase)
│   ├── skill_catalog.py       #   Skill 目录扫描、解析与校验
│   ├── progress_bus.py        #   进度事件总线 (LLM heartbeat → SSE → Web UI 实时渲染)
│   ├── service_launcher.py    #   后台服务 detach 启动器 (跨平台 CREATE_NO_WINDOW)
│   ├── task_mutation_guard.py #   任务状态变更安全护栏 (状态机合法性校验)
│   ├── task_template.py       #   任务模板合规校验
│   ├── web_events.py          #   Web UI SSE 事件编码
│   ├── models.py              #   共享数据模型 (Task, Session, Project)
│   ├── logger.py              #   结构化日志 (按模块分级)
│   ├── output.py              #   终端输出格式化 (Rich markup 安全转义)
│   ├── error_messages.py      #   错误消息模板
│   ├── paths.py               #   全局/项目存储路径约定
│   ├── console_encoding.py    #   Windows 控制台编码修复
│   ├── cli_progress.py        #   CLI 进度条组件
│   ├── gitignore.py           #   .gitignore 管理
│   ├── text_decode.py         #   子进程输出解码
│   └── ...
├── ai_support/                # AI 接入层
│   ├── providers.py           #   CLI + API Provider 注册表与生命周期 (CLI_PROVIDERS / API_PROVIDERS)
│   ├── provider_adapters.py   #   Provider 适配器 (OpenAI/Anthropic/DeepSeek SDK 统一接口)
│   ├── provider_profiles.py   #   Provider 能力描述 (模型、token 限制、cost 参数)
│   ├── provider_registry.py   #   Provider 注册与查找
│   ├── cli_families.py        #   CLI Agent 族注册 (claude/codex/opencode 三族 + EnvBridge)
│   ├── family_runtime.py      #   Agent 运行时环境 (API key bridge、命令解析)
│   ├── gateway_options.py     #   AI 网关调用选项聚合
│   ├── intent_classifier.py   #   意图分类器 (需求/问答/命令 三分类)
│   ├── intent_rules.py        #   启发式意图匹配规则 (中文关键词/句式)
│   ├── classifier.py          #   任务分类器 (优先级/类型/复杂度)
│   ├── task_planning.py       #   任务规划引擎 (spec→结构化任务列表)
│   ├── planner_context.py     #   规划器上下文构建 (项目元数据/文件树/规范摘要)
│   ├── planner_execution.py   #   规划执行调度 (schema-prompt / CLI fallback)
│   ├── planner_parse.py       #   规划结果解析 (JSON schema → task dict)
│   ├── clarification_protocol.py # 需求澄清协议 (多轮 Q&A→spec)
│   ├── clarify.py             #   澄清流程实现
│   ├── main_execute.py        #   主执行管线 (规划→任务创建→执行调度)
│   ├── main_resolution.py     #   主解析管线 (需求→agent→配置→执行器)
│   ├── interaction_controller.py # 交互控制器 (chat session 生命周期)
│   ├── question_answering.py  #   问答处理 (上下文检索→答案生成)
│   ├── question_runtime.py    #   问答运行时 (OpenCode 会话内 Q&A)
│   ├── opencode_runtime.py    #   OpenCode 运行时管理 (启动/停止/配置注入)
│   ├── agent_manifest.py      #   AI_MANIFEST.json 生成
│   ├── agent_guides.py        #   AI_USAGE.md 生成
│   ├── agent_commands.py      #   机器可读命令清单
│   ├── agent_support.py       #   Agent 支持工具 (模板/校验/构建)
│   ├── agent_task_template.py #   任务模板校验 (必填字段/格式)
│   ├── backlog_dedup.py       #   Backlog 去重 (标题/文件相似度)
│   ├── project_metadata.py    #   项目元数据提取 (语言/框架/依赖)
│   ├── prompts.py             #   Prompt 模板加载与渲染
│   ├── result_parse.py        #   执行结果解析 (exit code/output/summary)
│   └── service.py             #   AI 服务统一入口 (normalize_agent_name 等)
├── gateway/                   # AI API 网关
│   ├── api.py                 #   API 调用入口 (try_api → resolve → execute → format)
│   ├── resolution.py          #   Provider 解析与路由 (配置→provider key→adapter)
│   ├── execute.py             #   API 执行 (HTTP POST + SDK invoke)
│   ├── prompt_build.py        #   Prompt 构造 (system/user message 拼接)
│   ├── entrypoints.py         #   入口点注册 (dispatcher: CLI vs API)
│   ├── call_skeleton.py       #   调用骨架 (请求/响应标准化)
│   ├── service.py             #   网关服务
│   └── types.py               #   网关类型 (GatewayMode/GatewayRequest/GatewayResponse)
├── storage/                   # 持久化层 (SQLite)
│   ├── database.py            #   数据库初始化、连接池、缓存、查询函数
│   ├── schema_store.py        #   Schema 版本管理 (baseline + 增量迁移)
│   ├── task_write_store.py    #   任务增删改 (CRUD + 状态流转)
│   ├── task_read_model.py     #   任务查询 (分页/过滤/排序/统计)
│   ├── session_store.py       #   会话存储 (消息/会话 CRUD)
│   ├── project_store.py       #   项目注册 (upsert/delete/fetch)
│   └── service_state_store.py #   服务状态存储 (PID/端口/健康)
├── mcp/                       # MCP (Model Context Protocol) 服务
│   ├── server.py              #   MCP 服务器 (FastMCP 绑定、工具注册、异步执行)
│   ├── tool_registry.py       #   工具注册表 (声明式 ToolDefinition → MCP schema)
│   ├── protocol.py            #   协议适配 (错误/进度标准化)
│   ├── stdio_guard.py         #   Stdio 安全护栏 (敏感字段过滤、长度限制)
│   ├── audit.py               #   工具调用审计日志
│   ├── launchers/             #   MCP 启动器 (OpenCode MCP 配置生成)
│   ├── tools/tasks/           #   任务类 MCP 工具 (8 个)
│   │   ├── create_task.py     #     创建任务 (模板校验 + task_mutation_guard)
│   │   ├── list_tasks.py      #     列出任务 (分页/过滤/排序)
│   │   ├── show_task.py       #     查看任务详情
│   │   ├── edit_task.py       #     编辑任务字段
│   │   ├── stop_task.py       #     停止运行中任务
│   │   ├── archive_task.py    #     归档已完成任务
│   │   ├── validate_task_template.py # 校验任务模板
│   │   └── generate_breakdown.py     # 生成任务拆分建议
│   ├── tools/context/         #   上下文类 MCP 工具 (6 个)
│   │   ├── explore.py         #     项目探索 (grep/glob 只读)
│   │   ├── inspect_project.py #     项目巡检触发
│   │   ├── wiki_query.py      #     Wiki 知识库查询
│   │   ├── wiki_add.py        #     Wiki 条目添加
│   │   ├── note_add.py        #     项目笔记添加
│   │   ├── hook_trigger.py    #     Hook 触发
│   │   └── workflow.py        #     工作流状态读写
│   ├── tools/ops/             #   运维类 MCP 工具 (4 个)
│   │   ├── exec.py            #     受控命令执行
│   │   ├── doctor.py          #     健康诊断
│   │   ├── daemon_status.py   #     守护进程状态
│   │   ├── build_fix.py       #     构建修复
│   │   └── run_once.py        #     单次任务执行
│   └── tools/external/        #   外部集成 MCP 工具 (2 个)
│       ├── feishu_notify.py   #     飞书消息推送
│       └── webhook_invoke.py  #     Webhook 回调
├── webapp/                    # Web UI 后端 (HTTP + SSE)
│   ├── server.py              #   ThreadingHTTPServer 主体 + SSE 推送 + 静态资源服务
│   ├── actions.py             #   动作总入口 (重导出)
│   ├── action_requirements.py #   需求提交/分发 (goal→intent→dispatch)
│   ├── action_sessions.py     #   会话管理 (创建/消息/停止/权限)
│   ├── action_session_history.py # 会话历史
│   ├── action_session_records.py  # 会话记录
│   ├── action_task_ops.py     #   任务批量操作 (创建/删除/归档/取消)
│   ├── action_state.py        #   UI 共享状态 (jobs/events 内存管理)
│   ├── action_workflow.py     #   工作流动作 (artifact next_actions)
│   ├── payloads.py            #   页面负载构造 (dashboard/goal/sessions)
│   ├── task_payloads.py       #   任务负载构造 (list/detail)
│   ├── live_output_payloads.py #  实时输出负载
│   ├── display_sort.py        #   任务排序规则
│   ├── schema.py              #   请求/响应 JSON Schema
│   └── webhook.py             #   Webhook HTTP 端点
├── web/                       # Web UI 前端 (单页应用)
│   ├── index.html             #   主页面骨架
│   ├── app.js                 #   应用入口与路由
│   ├── styles.css             #   全局样式
│   ├── utils.js               #   工具函数 (API 调用/格式化)
│   ├── components/            #   UI 组件 (ChatView, Composer, GoalInput)
│   └── boundaries/            #   边界组件 (Session, State, Submission)
├── feishu_bot/                # 飞书/Lark 机器人
│   ├── command_handlers.py    #   命令分发 (文本消息 → 命令路由)
│   ├── card_builders.py       #   卡片构造 (任务/项目/会话 30+ 卡片模板)
│   ├── session_runtime.py     #   OpenCode 会话绑定
│   ├── helpers.py             #   辅助函数 (项目解析/待确认/去重)
│   └── constants.py           #   常量定义
├── feishu_cards.py            # 飞书卡片底层组件 (section/field/note/button)
├── feishu_commands.py         # 飞书命令解析 (任务ID/状态/参数)
├── feishu_config.py           # 飞书 Bot 配置 (AppID/Secret/Node 路径)
├── feishu_interactions.py     # 飞书交互回调解析
├── feishu_runtime.py          # 飞书 Node.js 运行时管理 (sidecar 启动/停止)
├── feishu_worker.mjs          # 飞书长连接 Worker (WebSocket → 事件循环)
├── feishu_notify.mjs          # 飞书通知推送 (HTTP API 调用)
├── opencode/                  # OpenCode TUI 集成
│   ├── config.py              #   OpenCodeConfig 数据模型
│   ├── env.py                 #   环境变量桥接 (CODEPILOT_* → OPENCODE_*)
│   ├── paths.py               #   运行时路径 (~/.codepilot/opencode/<项目>/)
│   ├── profile.py             #   TUI Profile 生成 (opencode.json + tui.json + config/*)
│   ├── session.py             #   会话隔离 (per-project session ID)
│   └── model_state.py         #   用户模型选择持久化
├── scheduled/                 # 定时/事件 Agent
│   ├── runner.py              #   Agent 作业执行 (CLI subprocess + 输出解析)
│   ├── daemon.py              #   调度守护进程 (间隔 + Cron 表达式)
│   ├── guards.py              #   成本护栏 (per-job / daily USD cap) + 循环熔断
│   ├── audit.py               #   审计日志 (.codepilot/scheduled/audit.jsonl)
│   ├── triggers.py            #   事件触发器 (task.failed / task.done)
│   └── templates.py           #   内置 Agent 模板 (task_health / daily_summary / auto_inspect)
├── binary_support/            # 二进制构建与发布
│   ├── manager.py             #   PyInstaller 构建 (spec 生成/打包/校验)
│   ├── release.py             #   发布流程 (版本号/变更日志/产物上传)
│   ├── vendor_fetcher.py      #   第三方 CLI 抓取 (GitHub Release → 缓存 → bundled)
│   ├── paths.py               #   路径计算 (安装目录/资源目录)
│   └── version.py             #   版本号管理
├── codex/                     # Codex CLI 会话持久化 (JSON 文件存储)
├── claude/                    # Claude Code 会话持久化 (JSON 文件存储)
├── mcp/launchers/             # MCP 启动器 (OpenCode MCP 配置)
├── templates/                 # 任务模板 Markdown (builder/reviewer/repl 默认 prompt)
├── prompts/                   # Prompt 模板 (planner/clarifier/classifier 系统提示词)
└── nl_command_router.py       # 自然语言命令路由 (中/英文关键词→结构化命令)
```

### 数据流概要

```
┌─────────────────────────────────────────────────────┐
│              多通道输入                              │
│  CLI (codepilot go)  Web UI  飞书   Webhook        │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  nl_command_router / intent_classifier              │
│  意图识别 → 需求(requirement) / 问答(question)       │
│           / 操作(command) / 巡检(inspect)            │
└──────────────────────┬──────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   clarify          plan           auto
   (模糊→spec)     (spec→任务)    (端到端)
        │              │              │
        └──────────────┼──────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  run_orchestrator  (任务队列编排)                    │
│  ├─ 项目上下文解析 + 配置加载                         │
│  ├─ 任务 workspace 准备 (direct / branch / worktree) │
│  ├─ executor 选择 (builtin / dispatch / auto)         │
│  └─ 结果收尾 (commit/merge/cleanup + event emit)     │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  run_builtin_executor  (内置双阶段执行引擎)           │
│  ┌─────────────────────────────────────────────┐    │
│  │  Phase 1: Builder                           │    │
│  │  codex/claude/opencode exec <prompt>         │    │
│  │  → 生成/修改代码, 运行测试                    │    │
│  └──────────────┬──────────────────────────────┘    │
│                 │                                    │
│  ┌──────────────▼──────────────────────────────┐    │
│  │  Phase 2: Reviewer                          │    │
│  │  codex review --uncommitted                  │    │
│  │  → 审查改动, 输出 PASS/FAIL verdict           │    │
│  └──────────────┬──────────────────────────────┘    │
│                 │                                    │
│     ┌───────────┴───────────┐                       │
│     ▼                       ▼                       │
│   PASS                   FAIL                       │
│   → done                 → reach max_rounds?        │
│                            │ yes      │ no           │
│                            ▼          ▼              │
│                          failed    backlog           │
│                          (triage)  (builder retry)   │
└─────────────────────────────────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
       codex        claude       opencode
         │             │             │
         └─────────────┼─────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  gateway (AI API 网关)                               │
│  resolution → execute → format                      │
│  ├─ CLI mode: subprocess 调 CLI agent               │
│  └─ API mode: HTTP POST → OpenAI/Anthropic/DeepSeek │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  storage (SQLite ~/.codepilot/data.db)              │
│  tasks │ sessions │ projects │ service_states        │
│  memory events (.codepilot/memory/)                  │
│  scheduled audit/guards (.codepilot/scheduled/)      │
└─────────────────────────────────────────────────────┘
```

### 关键设计决策

- **双阶段 Agent 闭环** (`run_builtin_executor.py`)：每个任务由 builder 实现、reviewer 审查。reviewer 输出结构化 PASS/FAIL verdict，FAIL 时 builder 收到 reviewer 反馈后重试，最多循环 `max_review_rounds` 轮。超过上限后进入确定性失败分诊流程。
- **失败分诊管线** (`run_failure_triage*.py`)：任务失败时采集证据 (exit code/stderr/agent output/task context)，由分类器决定 action (retry_with_hint / replan / discard / merge_partial)，通过 LLM prompt 构造修复指令后自动执行。
- **工作流状态机** (`core/workflow_state.py`)：项目级 mode state 管理 clarify → plan → execute 三阶段流转，每个阶段产出结构化 artifact (spec/plan/context) 以原子 JSON 文件持久化在 `.codepilot/state/`，支持断点续跑。
- **记忆系统** (`core/memory.py`)：append-only 事实事件日志 → 去重候选生成 → 反馈评分 (positive/negative/neutral) → 合并 seen_count → 自动沉淀为 `autocapture.md`。事件源覆盖 task 状态变更、workflow action 执行、inspect 报告反馈等。
- **MCP 工具三层架构**：任务类 (受 `task_mutation_guard` 状态机保护) → 上下文类 (只读, 不修改文件) → 外部集成类 (桥接飞书/Webhook)。每层有独立的审计和安全策略。
- **Agent 族兜底链**：`fallback_cli_order` 定义 CLI agent 优先级列表。任一 agent 不可用时 (未安装/无 key/超时) 自动切换至下一个。双阶段执行中 builder 使用的 agent 失败时，尝试交换 builder/reviewer agent 或降级到其他可用族。
- **三层配置叠加**：`~/.codepilot/AGENTS.toml` (全局默认) → 项目 `AGENTS.toml` (按项目覆盖) → `.codepilot.secrets.toml` (API key 等敏感值，不入 git)。加载时有 schema 校验和智能错误提示。
- **任务工作区隔离**：支持三种模式 — `direct` (主目录直接执行)、`branch` (git 分支隔离)、`worktree` (独立 git worktree，链接 node_modules 等依赖目录)。preflight 脏工作区策略可配置为 stop/commit/stash。
- **OpenCode 品牌隔离**：启动时在 `~/.codepilot/opencode/<项目>/` 生成完整的运行时配置 (MCP server/tools/permissions/TUI plugin/instructions)，将 CodePilot 的品牌、Agent 定义和权限策略注入官方 OpenCode 二进制，不修改 OpenCode 源码。

### 测试体系

项目遵循 TDD 方法论，约 150+ 测试文件覆盖所有核心模块：

| 层级 | 说明 | 运行方式 |
| --- | --- | --- |
| **单元测试** | 每个模块的独立测试，mock 外部依赖 | `pytest tests/ -m "not slow"` (日常 ~60-90s) |
| **集成测试** | 跨模块边界测试 (DB + CLI + MCP + Web UI API) | `pytest tests/` 全量 (~3.5min with xdist) |
| **慢速测试** | 涉及 git worktree、完整 pipeline、自迭代的 >10s 用例 | `pytest tests/ -m "slow"` (标记 `slow`) |
| **串行测试** | 持有全局锁/端口/daemon 的用例 | 标记 `serial`，xdist 同 worker 执行 |
| **E2E** | Web UI 资产完整性、二进制构建/安装/发布流程 | `pytest tests/test_web_assets.py tests/test_workflow_binary_release.py` |

测试基础设施：`conftest.py` 提供共享 fixture (临时项目/数据库/配置)；`*_testkit.py` 提供可复用测试工具 (AI gateway mock、feishu bot mock、chat flow builder、MCP stdio shim)。

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

- **快速上手：[中文](docs/01-快速上手指南.zh-CN.md) / [English](docs/01-quickstart-guide.en-US.md)**
- 说明文档：[中文](docs/02-说明文档.zh-CN.md) / [English](docs/02-overview.en-US.md)
- 操作文档：[中文](docs/03-操作文档.zh-CN.md) / [English](docs/03-operation-guide.en-US.md)
- AI / Agent 调用手册：[中文](docs/04-AI与Agent调用手册.zh-CN.md) / [English](docs/04-ai-agent-manual.en-US.md)
- Skill 化集成指南：[中文](docs/05-Skill化集成指南.zh-CN.md) / [English](docs/05-skill-integration-guide.en-US.md)
- 项目服务说明：[中文](docs/06-项目服务改造说明.zh-CN.md) / [English](docs/06-project-services.en-US.md)
- 工作流状态约定：[中文](docs/07-workflow-state.zh-CN.md) / [English](docs/07-workflow-state.en-US.md)
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

该 Skill 用英文编写，供其他 Codex / Agent 固化 CodePilot 的调用策略、命令顺序和排障流程。安装和维护方式见：[Skill化集成指南](docs/05-Skill化集成指南.zh-CN.md)

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
