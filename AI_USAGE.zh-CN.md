# CodePilot AI 调用手册

这份手册是写给其他 AI/Agent 的，不是写给人类终端用户的。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时，优先使用 JSON 输出命令。
3. 需要提交高层需求时，直接调用自然语言入口，不要先自己拆任务，除非你明确要控制拆分策略。
4. 看到任务处于 `in_progress` 时，先查 `status -v` 和 `logs`，不要盲目重复触发 `run`。
5. 任务失败或取消后，如需人工重新排队，使用 `codepilot retry <task_id>`。
6. 准备发布包时，优先使用 `codepilot release prepare --version <版本号>`。

## 推荐命令

### 1. 初始化项目

```bash
codepilot init .
```

### 2. 提交一个需求

```bash
codepilot "修复任务重试逻辑并补测试"
```

或者显式：

```bash
codepilot go "修复任务重试逻辑并补测试"
```

### 3. 查看任务状态

人类可读：

```bash
codepilot status -p <项目名> -v
```

机器可读：

```bash
codepilot status -p <项目名> --json
```

### 4. 精确查看单个任务

```bash
codepilot show <task_id>
codepilot show <task_id> --json
```

### 5. 查看日志

```bash
codepilot logs <task_id>
codepilot logs <task_id> --tail 80
```

### 6. 停止任务

```bash
codepilot stop <task_id>
```

### 7. 手动重试任务

```bash
codepilot retry <task_id>
```

### 8. 发布

最推荐：

```bash
codepilot release prepare --version 0.1.1
```

只打包：

```bash
codepilot release bundle --build-current
```

校验发布目录：

```bash
codepilot release verify
```

### 9. 图形界面

```bash
codepilot ui
```

## 结构化接口

### 命令清单 JSON

```bash
codepilot ai manifest
```

### AI 手册 Markdown

```bash
codepilot ai guide
```

### 给其他 AI 的短提示

```bash
codepilot ai prompt
```
