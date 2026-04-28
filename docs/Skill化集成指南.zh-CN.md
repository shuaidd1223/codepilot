# CodePilot Skill 化集成指南

本文说明如何把 CodePilot 作为 Skill 提供给其他 AI / 智能体调用。

## 1. Skill 目录

仓库内 Skill 包路径：

- `skills/codepilot-workflow/`

建议结构：

- `SKILL.md`：主流程与触发说明
- `agents/openai.yaml`：界面与默认调用提示
- `references/`：命令集合与执行模板

约定：`skills/codepilot-workflow/` 内所有面向 Agent 的内容使用英文编写；仓库 `docs/` 下的人类说明文档继续使用中文。

## 1.1 安装到本机 Skill 目录（可选）

如果你的 Agent 框架按 `$CODEX_HOME/skills` 自动发现技能，可把该目录复制过去：

```bash
# Linux/macOS
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/codepilot-workflow "${CODEX_HOME:-$HOME/.codex}/skills/"
```

```powershell
# Windows PowerShell
$target = Join-Path ($env:CODEX_HOME ? $env:CODEX_HOME : \"$HOME\\.codex\") \"skills\"
New-Item -ItemType Directory -Force $target | Out-Null
Copy-Item -Recurse -Force .\\skills\\codepilot-workflow $target
```

## 1.2 从 Git 仓库安装给其他 AI 使用

不一定要把 skill 单独拆成一个项目。只要当前仓库发布到 GitHub/Git 服务，其他 Codex 实例可以直接从仓库子目录安装：

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo <owner>/<repo> \
  --path skills/codepilot-workflow
```

如果使用 GitHub URL：

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --url https://github.com/<owner>/<repo>/tree/main/skills/codepilot-workflow
```

Windows PowerShell 示例：

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" `
  --repo <owner>/<repo> `
  --path skills/codepilot-workflow
```

安装后需要重启 Codex 才会加载新 skill。

如果对方不是 Codex，也可以用 Git sparse-checkout 只拉 skill 目录：

```bash
git clone --filter=blob:none --sparse https://github.com/<owner>/<repo>.git codepilot-skill
cd codepilot-skill
git sparse-checkout set skills/codepilot-workflow
cp -R skills/codepilot-workflow "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Windows PowerShell：

```powershell
git clone --filter=blob:none --sparse https://github.com/<owner>/<repo>.git codepilot-skill
Set-Location codepilot-skill
git sparse-checkout set skills/codepilot-workflow
$target = Join-Path ($env:CODEX_HOME ? $env:CODEX_HOME : "$HOME\.codex") "skills"
New-Item -ItemType Directory -Force $target | Out-Null
Copy-Item -Recurse -Force .\skills\codepilot-workflow $target
```

## 1.3 单独拆成 Skill 项目的发布方式

如果未来要把 skill 单独拎成一个 Git 仓库，仓库根目录建议只保留 skill 本体和最少说明：

```text
codepilot-workflow-skill/
├── codepilot-workflow/
│   ├── SKILL.md
│   ├── agents/
│   │   └── openai.yaml
│   └── references/
│       ├── command-map.md
│       └── agent-playbooks.md
└── README.md
```

安装命令对应改成：

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo <owner>/codepilot-workflow-skill \
  --path codepilot-workflow
```

拆独立仓库的好处是安装路径更短、权限更清晰；保留在主仓库的好处是 skill 和 CodePilot 命令文档更容易同步。当前阶段推荐先保留在主仓库，通过 `--path skills/codepilot-workflow` 安装。

## 2. 触发场景建议

当用户请求包含以下意图时应触发该 Skill：

- “把需求转成任务并执行”
- “回答当前项目状态、任务数量、失败任务、运行中任务”
- “查看任务状态 / 日志 / 停止 / 重试”
- “运行项目队列或后台 daemon”
- “启动/排障飞书机器人或 webhook”
- “打包发布当前工具”

## 3. 推荐调用方式

显式调用示例：

```text
Use $codepilot-workflow at /path/to/skills/codepilot-workflow to handle this repository workflow request.
```

默认优先执行链路：

1. `codepilot "需求文本"`
2. `codepilot status -p <项目名> --json`
3. `codepilot task show <task_id> --json`
4. `codepilot task logs <task_id> --tail 80`

问答链路：

1. `codepilot go "当前项目状态怎么样" -p <项目名>`
2. 必要时读取 `codepilot status -p <项目名> --json`
3. 只在用户明确要求创建工作时进入需求/任务流程

交互渠道（`chat`、Web UI 会话、飞书自由文本）：

- `? <问题>`：问答
- `# <内容>`：创建需求
- `! <内容>`：创建单步任务
- 没有显式前缀的疑似需求/任务只会得到确认提示，不会直接执行

## 4. 跨 Agent 的最小约束

- 禁止使用已移除旧命令：`release`、顶层 `show/logs/stop/retry/...`
- 一律使用：`task` 与 `binary` 分组
- 可机读需求优先 `--json`
- Skill 包内容必须保持英文，避免把中文操作说明写进 `skills/` 目录
- 项目状态、任务统计、运行服务等问题优先当问答处理，不要因为出现“优化/修复”等词就直接创建任务

## 5. 维护建议

每次命令结构调整后，至少同步更新：

- `skills/codepilot-workflow/SKILL.md`
- `skills/codepilot-workflow/references/command-map.md`
- `skills/codepilot-workflow/references/agent-playbooks.md`
- `skills/codepilot-workflow/agents/openai.yaml`
- `AI_MANIFEST.json`
- `AI_USAGE.zh-CN.md`
- `docs/AI与Agent调用手册.zh-CN.md`
- `docs/操作文档.zh-CN.md`
- `docs/说明文档.zh-CN.md`

## 6. 验证建议

建议在接入端做三类冒烟：

1. 需求提交：`codepilot "..."`
2. 状态查询：`codepilot status -p ... --json`
3. 问答确认：`codepilot go "当前项目状态怎么样" -p ...`
4. 任务控制：`codepilot task show/logs/retry`
5. 集成服务：`codepilot feishu status` / `codepilot webhook --help`
