# CodePilot Skill 化集成指南

语言版本：中文 | [English](05-skill-integration-guide.en-US.md)

本文说明如何把 CodePilot 作为 Skill 提供给其他 AI / Agent 调用，并保持 Skill 与当前 CLI 命令结构同步。

## 1. Skill 目录

仓库内 Skill 包路径：

- `skills/codepilot-workflow/`

结构：

- `SKILL.md`：触发说明、硬约束和主流程
- `agents/openai.yaml`：界面展示元数据和默认提示
- `references/command-map.md`：命令分组和最小命令集
- `references/agent-playbooks.md`：常见 Agent 操作流程

约定：`skills/codepilot-workflow/` 内面向 Agent 的内容使用英文；仓库 `docs/` 下的人类说明文档使用中文。

## 2. 安装方式

### 2.1 复制到本机 Skill 目录

Linux / macOS：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/codepilot-workflow "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Windows PowerShell：

```powershell
$target = Join-Path ($env:CODEX_HOME ? $env:CODEX_HOME : "$HOME\.codex") "skills"
New-Item -ItemType Directory -Force $target | Out-Null
Copy-Item -Recurse -Force .\skills\codepilot-workflow $target
```

安装后重启 Codex / Agent 进程。

### 2.2 从 Git 仓库子目录安装

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo <owner>/<repo> \
  --path skills/codepilot-workflow
```

或使用 GitHub URL：

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --url https://github.com/<owner>/<repo>/tree/main/skills/codepilot-workflow
```

Windows PowerShell：

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" `
  --repo <owner>/<repo> `
  --path skills/codepilot-workflow
```

### 2.3 sparse-checkout 安装

```bash
git clone --filter=blob:none --sparse https://github.com/<owner>/<repo>.git codepilot-skill
cd codepilot-skill
git sparse-checkout set skills/codepilot-workflow
cp -R skills/codepilot-workflow "${CODEX_HOME:-$HOME/.codex}/skills/"
```

## 3. 触发场景

当用户请求包含以下意图时应触发该 Skill：

- 把需求转成任务并执行
- 回答当前项目状态、任务数量、失败任务、运行中任务
- 生成澄清规格或执行计划
- 只读取证、查询 wiki、读取 note、查看 trace
- 查看任务详情、日志、停止、重试、恢复、取消、归档
- 运行 backlog、daemon、inspect、Web UI
- 启动或排障飞书、Webhook、事件、Hook、Provider
- 管理本地 skill catalog
- 构建、安装、校验或准备二进制发布

## 4. 推荐调用链

### 4.1 新需求

```text
Use $codepilot-workflow to submit and track this requirement.
```

默认命令：

1. `codepilot "需求文本"`
2. `codepilot status -p <项目名> --json`
3. `codepilot task show <task_id> --json`
4. `codepilot task logs <task_id> --tail 80`

### 4.2 先澄清再计划

1. `codepilot clarify -p <项目名> "模糊需求" --json`
2. `codepilot plan -p <项目名> --from-spec <spec_path> --json`
3. 人工确认后再创建工作

### 4.3 项目问答

1. `codepilot go "当前项目状态怎么样" -p <项目名>`
2. 必要时读取 `codepilot status -p <项目名> --json`
3. 需要证据时读取 `codepilot explore -p <项目名> --prompt "问题" --json`
4. 不要从探索性或含糊表达直接创建任务

### 4.4 交互渠道

`chat`、Web UI 会话、飞书自由文本统一进入 OpenCode + CodePilot MCP。外部 Agent 可以直接表达问题、需求或操作意图；需要严格产出 artifact 时，再显式调用 `clarify` / `plan` / MCP 工具。

CodePilot 启动的 OpenCode 使用用户级隔离运行时 `~/.codepilot/opencode/<项目标识>/`，保存生成配置、TUI 插件、会话数据和项目级模型选择，不污染用户自己直接运行的 OpenCode。

## 5. 跨 Agent 最小约束

- 禁止使用旧入口：`release`、顶层 `show/logs/stop/retry/find/...`、`webui`。
- 一律使用：`task`、`binary`、`ui` 分组。
- 机器可读调用优先加 `--json`。
- 项目状态、任务统计、运行服务等问题优先当问答处理。
- `explore`、`clarify`、`plan` 不应用作执行入口。
- `chat` / Web UI / 飞书自由文本由 OpenCode 会话调用 CodePilot MCP；需要确定性输出时显式调用结构化 CLI/MCP 工具。
- Skill 包内容保持英文，中文操作说明写在 `docs/`。
- 直接投递任务前必须读取 `codepilot ai template --format json`。

## 6. 维护清单

每次命令结构调整后，至少同步：

- `README.md`
- `docs/说明文档.zh-CN.md`
- `docs/操作文档.zh-CN.md`
- `docs/AI与Agent调用手册.zh-CN.md`
- `docs/Skill化集成指南.zh-CN.md`
- `AI_USAGE.zh-CN.md`
- `AI_MANIFEST.json`
- `skills/codepilot-workflow/SKILL.md`
- `skills/codepilot-workflow/references/command-map.md`
- `skills/codepilot-workflow/references/agent-playbooks.md`
- `skills/codepilot-workflow/agents/openai.yaml`

同步前建议运行：

```bash
codepilot --help
codepilot ai manifest
codepilot ai guide
```

## 7. 验证建议

文档和 Skill 更新后，至少做这些冒烟：

```bash
codepilot --help
codepilot task --help
codepilot ai manifest
codepilot ai guide
codepilot status -p <项目名> --json
codepilot explore -p <项目名> --prompt "task template" --json
```

如果只改文档和 Skill，不需要跑全量 pytest；交付说明中应写明未跑自动化测试的原因，并给出上面的人工验证命令。
