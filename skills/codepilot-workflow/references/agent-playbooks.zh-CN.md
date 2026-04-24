# Agent 调用 Playbooks

## Playbook A：提需求并跟踪

1. 执行：`codepilot "<requirement>"`
2. 查询：`codepilot status -p <project> --json`
3. 定位任务：读取 `tasks[].id`
4. 详情：`codepilot task show <task_id> --json`
5. 进展：`codepilot task logs <task_id> --tail 80`

## Playbook B：任务失败后的恢复

1. `codepilot task logs <task_id> --full`
2. 判断是否环境问题（依赖、权限、凭据、工作区脏状态）
3. 必要时先 `codepilot task stop <task_id>`
4. `codepilot task retry <task_id>`
5. `codepilot run -p <project>`

## Playbook C：项目后台运行

1. 启动：`codepilot daemon -p <project>`
2. 监控：`codepilot daemon -p <project> --status`
3. 停止轮询：`codepilot daemon -p <project> --stop`

## Playbook D：发布前打包

1. `codepilot binary prepare --version <version>`
2. `codepilot binary verify`
3. 若需要仅打包：`codepilot binary release --build-current`

## 错误处理约定

- 若出现“命令已移除”，立即切换到 `task` / `binary` 新分组。
- 若需要机器可读结果，一律加 `--json`。
- 若调用失败含 `error.code`，优先按 `error.code` 分支处理。
