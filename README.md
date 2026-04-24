# CodePilot

CodePilot 是一个本地工程工作流 CLI：把自然语言需求转换成任务，并按项目完成规划、执行、审查、发布与运维控制。

## 快速开始

初始化项目：

```bash
codepilot init .
```

提交一个需求（自动判断是否拆分）：

```bash
codepilot "实现自动拆分和自动执行工作流"
```

查看状态：

```bash
codepilot status -p <项目名> -v
```

任务运维（统一入口 `task`）：

```bash
codepilot task show <task_id>
codepilot task logs <task_id>
codepilot task stop <task_id>
codepilot task retry <task_id>
```

发布与二进制（统一入口 `binary`）：

```bash
codepilot binary build
codepilot binary prepare --version 0.1.1
codepilot binary verify
```

说明：`codepilot release ...` 旧入口已移除，请统一使用 `codepilot binary ...`。

## 文档导航

- 说明文档：[docs/说明文档.zh-CN.md](docs/说明文档.zh-CN.md)
- 操作文档：[docs/操作文档.zh-CN.md](docs/操作文档.zh-CN.md)
- AI / Agent 调用手册与命令集合：[docs/AI与Agent调用手册.zh-CN.md](docs/AI与Agent调用手册.zh-CN.md)
- Skill 化集成指南：[docs/Skill化集成指南.zh-CN.md](docs/Skill化集成指南.zh-CN.md)
- 多项目服务改造说明（历史专题）：[docs/project-services.md](docs/project-services.md)

## 给其他 AI / Agent 的标准入口

机器可读命令清单：

```bash
codepilot ai manifest
```

AI 调用手册（Markdown）：

```bash
codepilot ai guide
```

短提示词：

```bash
codepilot ai prompt
```

仓库根目录静态产物：

- `AI_MANIFEST.json`
- `AI_USAGE.zh-CN.md`

## Skill 包

仓库提供了可复用 Skill：

- `skills/codepilot-workflow/SKILL.md`

可用于让其他模型/智能体在统一流程下调用 CodePilot。详细用法见：[docs/Skill化集成指南.zh-CN.md](docs/Skill化集成指南.zh-CN.md)
