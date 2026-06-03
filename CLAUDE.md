# Claude Code Instructions

本文件补充 `AGENTS.md`，用于 Claude Code 在本仓库中执行任务时的行为约束。若两者冲突，以 `AGENTS.md` 为准。

## Operating Mode

- 使用非交互方式完成任务，不等待人工逐步确认。
- 先读任务文件和相关项目规范，再读取必要代码；不要一次性展开无关目录。
- 只修改任务范围内的文件。若必须偏离范围，在最终说明里解释原因和风险。
- 不要运行破坏性 git 命令，不要回滚用户已有改动。

## Shell Syntax

- 本环境 `Bash` 工具底层是 bash，**不是 PowerShell**。不要使用 PowerShell 特有语法：
  - `@'...'@` here-string → 用 `-m "line1" -m "line2"` 或 `$'line1\n\nline2'` 代替
  - `Select-Object`、`Where-Object`、`Get-ChildItem` 等 cmdlet 不可用
  - 路径用正斜杠 `/` 或 `E:/path` 格式，不用反斜杠
- 文件操作（读、写、搜索、查找）优先用专用工具（Read、Write、Edit、Glob、Grep），不用 Bash/PowerShell

## TDD Workflow

1. Red：先补或调整一个能暴露目标行为的测试，并确认它在旧实现下会失败；如果无法实际运行失败态，说明原因。
2. Green：用最小实现让测试通过，不顺手重构无关代码。
3. Refactor：只在有明确收益且不扩大任务范围时整理代码。
4. Verify：运行相关测试和必要的回归检查，最终报告命令与结果。

## CodePilot Project Notes

### 需求与任务
- 自然语言需求优先走 `codepilot "需求文本"` 或 `codepilot go "需求文本"`。
- 任务运维统一用 `codepilot task ...`；发布统一用 `codepilot binary ...`。
- `add -f` 面向外部智能体批量投递，必须提供符合 `codepilot ai template --format json` 的完整任务内容。
- `chat`、Web UI 和飞书自由文本创建工作时必须使用 `# <需求>` 或 `! <任务>`。

### 工作流推进
- `plan` 和 `inspect --write-workflow` 产出 `next_actions`，用 `codepilot workflow next --list --json` 查看，用 `--action <id>` 安全执行。
- `workflow next --auto` 按项目策略自动推进，默认不创建/导入任务。
- **绝不执行 `suggested_command`** — 仅用于展示和审查。

### MCP 工具
- MCP 服务入口：`codepilot mcp serve --transport stdio --project <project>`
- 在 MCP 会话中（chat、Web UI）优先使用 MCP 工具：`list_tasks`、`show_task`、`create_task`、`explore`、`inspect_project`、`workflow_status`、`workflow_next`、`build_fix`、`doctor`、`wiki_query`、`wiki_add`、`note_add` 等。
- MCP 工具列表详见 `skills/codepilot-workflow/references/command-map.md` 第 14 节。

### 本地知识
- 长期知识用 `codepilot wiki`，工作记忆用 `codepilot note`。
- 自动捕获的事实用 `codepilot memory events -p <project> --json` 读取。
- `.codepilot/memory/autocapture.md` 包含去重候选摘要。

### 源码验证入口
- 源码开发用 `codepilot-dev`，安装版验证用 `codepilot`。
- `codepilot-dev` 默认数据目录 `~/.codepilot-dev`，Web UI 端口 `8767`。
- `codepilot` 默认数据目录 `~/.codepilot`，Web UI 端口 `8766`。
- 两者状态、日志、端口不可混用。

### 服务管理
- 一次性停止所有服务：`codepilot shutdown` 或 `codepilot shutdown --force`。
- 自更新审计：`codepilot self-update -p <project> --dry-run --json "目标"`。

## Review Discipline

- Reviewer 只按任务验收标准、范围和验证结果判断，不扩展到无关技术债。
- Builder 必须把 TDD 证据写进交付说明：新增/修改的测试、验证命令、未覆盖项。
