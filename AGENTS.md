# Agent Development Rules

本文件是仓库根目录的通用智能体规范，会被 CodePilot 规划器和内置执行器读取。所有 AI/Agent 在本仓库工作时必须优先遵守这里的规则。

## TDD Mode

- 默认采用 TDD：先为目标行为补一个能失败的测试，再做最小实现，最后跑相关测试直到通过。
- 如果任务只改文档、配置说明、提示词或无法合理写自动化测试，必须在交付说明里写明原因，并给出可执行的人工验证步骤。
- 修复 bug 时先写回归测试，测试必须能覆盖失败场景；不要只改实现。
- 新增功能时测试应覆盖主路径和至少一个关键边界；高风险改动要补回归测试。
- 不为追求测试而大改结构；测试和实现都保持最小范围。

## Context And Planning

- 规划阶段只依赖用户需求、目录结构、规范文档、README/docs 摘要、已有任务和真实文件路径索引。
- 不要把完整代码文件内容塞进规划提示；需要代码细节时，让执行智能体自己读取相关文件。
- 规划任务必须给出真实路径、验收标准、验证命令和风险边界；不要编造不存在的文件或模块。

## Implementation Rules

- 优先沿用现有模块边界、命名、错误处理和测试风格。
- 改动保持小而可审查；不做与任务无关的重构、格式化或依赖升级。
- 不提交密钥、令牌、个人配置或生成产物。
- 修改 CLI、Web UI、存储、任务调度、AI 网关、飞书/Webhook、发布流程时，必须考虑兼容性和回归测试。

## Validation

- Python 改动优先运行相关 `pytest`；范围不明确时运行 `pytest -q`。
- 前端资源改动至少做静态检查或浏览器手工验证，并在结果中说明。
- 交付说明必须包含改动摘要、涉及文件和验证结果；未运行的检查要说明原因。

## Project-Specific Guidance

### 测试配置
- 测试运行使用 pytest-xdist 并行（`-n auto --dist loadfile`），同文件内测试留同一 worker
- 日常开发跳过慢速测试：`pytest -m 'not slow'`
- 标记为 `slow` 的用例耗时 >10s，用于 git worktree / full pipeline / self iteration
- 标记为 `serial` 的用例必须串行执行（持有全局锁、共享 daemon、抢端口等）

### 任务执行模式（AGENTS.toml）
- 当前项目使用 `task_workspace = "direct"`：任务直接在主工作目录执行，不新建分支或 worktree
- `per_task_branch = false`：禁用每任务分支
- fallback_cli_order = `["codex", "opencode"]`：codex 不可用时自动降级到 opencode

### 关键入口
- 源码开发入口：`codepilot-dev`。所有在本仓库验证当前源码改动的智能体必须优先使用它，例如 `codepilot-dev ui start/status/restart/stop`、`codepilot-dev status -p codepilot-dev`。
- 正式安装入口：`codepilot`。仅在明确验证已安装版本、冻结二进制或发布包行为时使用；不要用它验证当前工作区源码改动。
- 入口隔离：`codepilot-dev` 默认使用 `~/.codepilot-dev` 和 Web UI 端口 `8767`；`codepilot` 默认使用 `~/.codepilot` 和 Web UI 端口 `8766`。不要混用两者的状态、日志或端口。
  - 端口解析优先级：1) 显式 `--port` 参数 → 2) `CODEPILOT_WEBUI_PORT` 环境变量 → 3) 根据 `CODEPILOT_HOME` 或 `sys.argv[0]` 自动选择（`.codepilot-dev` → 8767，否则 → 8766）。
  - 实现位置：`codepilot/commands/webui_service.py`（`_default_port()`）和 `codepilot/commands/daemon.py`（`_daemon_default_ui_port()`）。
  - 相关测试：`tests/test_webui_service.py` 中的 `test_webui_start_dev_defaults_to_port_8767_without_env_var` 和 `test_webui_default_port_is_8766_in_installed_mode`。
- Python 包 CLI 入口：`codepilot`（通过 pyproject.toml 定义）；仓库内命令行验证仍使用上面的 `codepilot-dev` wrapper。
- MCP 工具通过 `codepilot.mcp` 包提供
- OpenCode 交互入口：源码验证用 `codepilot-dev chat -p codepilot-dev -a opencode`，由 CodePilot 注入隔离配置、MCP、模型和权限
- Web UI：源码验证用 `codepilot-dev ui start`（默认 8767）；安装版验证才用 `codepilot ui start`（默认 8766）
- 飞书机器人：源码验证用 `codepilot-dev feishu start`

### 重要约束
- 不要在 `AGENTS.toml` 中配置 `codex_cmd` / `claude_cmd`（旧格式已废弃）
- 不要把 OpenCode 品牌/TUI/agent/commands 这类工具级定制写到业务项目配置；运行时文件属于用户级 `~/.codepilot/opencode/<项目标识>/`
- `chat`、Web UI 会话和飞书自由文本统一走 OpenCode + CodePilot MCP；需要结构化产物时显式调用 `plan` 或对应 MCP 工具
- 外部任务投递必须使用 `codepilot add -f` 并符合 `codepilot ai template --format json` 格式
- `workflow next` 的 `suggested_command` 仅供展示/审查，绝不自动执行；安全推进只用 `--action <id>` 或 `--auto`
- 自更新审计 `codepilot self-update --dry-run` 不创建任务、不修改代码，仅产出评估计划
- MCP 工具开发遵循 `codepilot/mcp/tools/` 下的分类结构（tasks/context/ops/external），新增工具需在对应 `__init__.py` 中注册
