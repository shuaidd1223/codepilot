# CodePilot AI 调用手册

这份手册是写给其他 AI/Agent 的，不是写给人类终端用户的。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时，优先使用 JSON 输出命令。
3. 需要提交高层需求时，直接调用自然语言入口，不要先自己拆任务，除非你明确要控制拆分策略。
4. 看到任务处于 `in_progress` 时，先查 `status -v` 和 `task logs`，不要盲目重复触发 `run`。
5. 任务失败或取消后，如需人工重新排队，使用 `codepilot task retry <task_id>`。
6. 准备发布包时，优先使用 `codepilot binary prepare --version <版本号>`。

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
codepilot task show <task_id>
codepilot task show <task_id> --json
```

### 5. 环境自检

```bash
codepilot doctor
codepilot doctor --json
```

### 6. 查看日志

```bash
codepilot task logs <task_id>
codepilot task logs <task_id> --tail 80
```

### 7. 停止任务

```bash
codepilot task stop <task_id>
```

### 8. 手动重试任务

```bash
codepilot task retry <task_id>
```

### 9. 发布

最推荐：

```bash
codepilot binary prepare --version 0.1.1
```

只打包：

```bash
codepilot binary release --build-current
```

校验发布目录：

```bash
codepilot binary verify
```

### 10. 图形界面

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

### 任务模板（外部规划专用）

如果你**不**走 CodePilot 的规划器，而是自己在外部规划好任务并通过 `add -f tasks.json` / `add -f tasks.md` 投递，
**必须按 task-template 格式准备 content，没有占位通道**。三种输出：

```bash
codepilot ai template               # 原始 task-template.md（含 {title} 等占位符）
codepilot ai template --format json  # 机器可读字段 schema + 批量导入格式
codepilot ai template --format guide # 中文填充指南（含示例）
```

**强制规则（v0.2 起 add 命令的硬约束）**：

1. **人工调用方** —— 不要直接 `add`。要新增任务请走 `codepilot "需求文本"`，由规划器拆分；要单独排一条具体任务也只是 `add -t "标题"`，由 `--agent` 指定的模型自动生成模板合规 content。
2. **AI / 智能体调用方** —— 必须满足下面之一：
   - 用 `add -f tasks.json`，每条带模板合规 `content`（缺章节直接拒）；
   - 用 `add -f tasks.md`，多个任务之间 `---` 分隔，每段都是完整 task-template；
   - 用 `add -t "标题"`，让 CodePilot 调用 AI 生成 content（同样会做合规校验）。
3. **`--no-ai` / `--allow-empty` 已废弃** —— 不再有空 content 的占位通道；老版本写入的占位任务 UI 上会提示按 `ai template --format json` schema 重新投递。
4. **章节骨架保留英文，章节正文用中文**；不要写「待补充」「TBD」「无」之类占位词。
