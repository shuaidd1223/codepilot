# Task #167 用户需求原文

## 原始需求

当前项目需要继续推进“执行链路与巡检链路的职责拆分重构”，并在拆分前先完成可复核的规划输入，避免再次出现“空输入直接进 builder/reviewer”。

本轮需求范围限定为以下模块，不扩展到其它方向：

- `codepilot/commands/run_shell.py`
- `codepilot/commands/run_builtin.py`
- `codepilot/commands/run.py` 与 `codepilot/commands/run_orchestrator.py` 的调用边界
- `codepilot/commands/inspect_signals.py` 与 `codepilot/commands/inspect.py` 的信号聚合边界
- `codepilot/web/components/AgentLog.js` 与 `codepilot/web/boundaries/AgentLog*.js` 的渲染/交互边界

## 目标行为

- 在不改变现有 CLI/WebUI 对外行为的前提下，进一步降低单文件复杂度与耦合。
- 新一轮规划产物必须是可单轮交付的原子任务，每个任务都指向明确文件并可验证。

## 成功判据

- 规划输出的每个任务都包含明确改动文件与可验证结果。
- 不再生成“仅角色描述、无输入上下文”的伪任务。
- 规划阶段能够明确引用本次侦察结论（文件位置、依赖关系、风险点）。
