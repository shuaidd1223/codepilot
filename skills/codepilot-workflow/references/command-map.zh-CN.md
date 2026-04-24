# CodePilot 命令集合（Skill 参考）

## 1. 需求入口

```bash
codepilot "<requirement>"
codepilot go "<requirement>"
codepilot chat
```

## 2. 状态查询

```bash
codepilot status -p <project> --json
codepilot task show <task_id> --json
codepilot task find <keyword> -p <project> --json
codepilot doctor --json
```

## 3. 任务运维（统一 task 分组）

```bash
codepilot task logs <task_id> --tail 80
codepilot task stop <task_id>
codepilot task retry <task_id>
codepilot task cancel <task_id>
codepilot task archive <task_id>
codepilot task edit <task_id> --status backlog
codepilot task rm <task_id>
```

## 4. 队列执行与服务

```bash
codepilot run -p <project>
codepilot daemon -p <project>
codepilot daemon -p <project> --status
codepilot daemon -p <project> --stop
codepilot inspect -p <project> --once
codepilot ui status
```

## 5. 发布与安装（统一 binary 分组）

```bash
codepilot binary build
codepilot binary install --binary <binary-path>
codepilot binary release --build-current
codepilot binary verify
codepilot binary prepare --version <version>
```

## 6. AI 对接入口

```bash
codepilot ai manifest
codepilot ai guide
codepilot ai prompt
```

## 7. 已移除写法（禁止）

- `codepilot release ...`
- 顶层 `codepilot show/logs/stop/retry/find/...`
