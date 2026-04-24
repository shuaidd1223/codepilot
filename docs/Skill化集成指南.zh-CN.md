# CodePilot Skill 化集成指南

本文说明如何把 CodePilot 作为 Skill 提供给其他 AI / 智能体调用。

## 1. Skill 目录

仓库内 Skill 包路径：

- `skills/codepilot-workflow/`

建议结构：

- `SKILL.md`：主流程与触发说明
- `agents/openai.yaml`：界面与默认调用提示
- `references/`：命令集合与执行模板

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

## 2. 触发场景建议

当用户请求包含以下意图时应触发该 Skill：

- “把需求转成任务并执行”
- “查看任务状态 / 日志 / 停止 / 重试”
- “运行项目队列或后台 daemon”
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

## 4. 跨 Agent 的最小约束

- 禁止使用已移除旧命令：`release`、顶层 `show/logs/stop/retry/...`
- 一律使用：`task` 与 `binary` 分组
- 可机读需求优先 `--json`

## 5. 维护建议

每次命令结构调整后，至少同步更新：

- `skills/codepilot-workflow/SKILL.md`
- `skills/codepilot-workflow/references/command-map.zh-CN.md`
- `AI_MANIFEST.json`
- `AI_USAGE.zh-CN.md`

## 6. 验证建议

建议在接入端做三类冒烟：

1. 需求提交：`codepilot "..."`
2. 状态查询：`codepilot status -p ... --json`
3. 任务控制：`codepilot task show/logs/retry`
