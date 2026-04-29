# CodePilot AI 调用手册

这份手册是写给其他 AI/Agent 的，不是写给人类终端用户的。

## 最重要的调用原则

1. 优先使用非交互命令。
2. 需要结构化结果时，优先使用 JSON 输出命令。
3. 需要提交高层需求时，直接调用自然语言入口，不要先自己拆任务，除非你明确要控制拆分策略。
4. 看到任务处于 `in_progress` 时，先查 `status -v` 和 `task logs`，不要盲目重复触发 `run`。
5. 任务失败或取消后，如需人工重新排队，使用 `codepilot task retry <task_id>`。
6. 准备发布包时，优先使用 `codepilot binary prepare --version <版本号>`。
7. 在 `chat`、Web UI 会话和飞书自由文本中，疑似需求/任务不会直接执行；创建工作必须显式输入 `需求 <内容>` / `# <内容>` 或 `任务 <内容>` / `! <内容>`。
8. 项目状态、任务数量、完成度、失败任务、运行中任务、服务状态这类问题应作为问答处理；CodePilot 会优先读取本地运行数据。

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

### 3.1 查看轻量 HUD

```bash
codepilot hud -p <项目名> --preset full
codepilot hud -p <项目名> --preset full --json
```

`hud` 适合快速判断当前工作台是否繁忙：它汇总项目队列、运行中任务、最近活动和后台服务状态；需要实时观察时使用 `hud --watch`。

### 3.2 只读探索项目证据

```bash
codepilot explore --prompt "find task template" --json
```

`explore` 只读取项目文件、Git、任务日志摘要和 inspect 信号。涉及修改、安装、启动服务或执行测试的问题应改走普通 workflow。

### 3.3 项目本地 wiki

```bash
codepilot wiki add -p <项目名> --title "构建命令" --body "pytest tests"
codepilot wiki query -p <项目名> "构建" --json
codepilot wiki lint -p <项目名> --json
```

适合写入 wiki 的内容包括稳定构建命令、架构事实、巡检发现、常见失败、人工决策和项目约定。不要写入 secret、API key、token、Feishu app_secret 或临时大段日志。

### 3.4 生成执行前需求规格

```bash
codepilot clarify -p <项目名> "改进 doctor" --json
```

`clarify` 只生成 `.codepilot/specs/clarify-*.md` 和 context artifact，写入 workflow state，不创建 backlog 任务、不启动执行器。适合先把模糊需求整理成目标、范围、非目标、约束、验收标准和待确认问题。

### 3.5 生成可审查执行计划

```bash
codepilot plan -p <项目名> "新增 explore" --json
codepilot plan -p <项目名> --from-spec .codepilot/specs/example.md --json
```

`plan` 生成 `.codepilot/plans/plan-*.md` 和 context artifact，返回任务候选、风险、执行顺序和验证矩阵。默认不创建 backlog、不启动执行器；人工确认后再导入任务或继续 clarify。

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

### 11. 交互会话的显式前缀

`chat`、Web UI 会话和飞书自由文本会优先保护执行边界：疑似需求/任务没有显式前缀时，只返回确认提示，不会创建任务。

```text
? 当前项目状态怎么样
问题 当前有多少任务，完成了多少
需求 优化飞书任务面板
# 修复任务通知卡片样式
任务 重跑失败任务 12
! 修复一个明确的小问题
```

### 12. 飞书与 Webhook

```bash
codepilot feishu start
codepilot feishu status
codepilot feishu logs --tail 100
codepilot feishu stop
codepilot webhook --host 127.0.0.1 --port 8765
```

飞书通知优先使用 interactive 卡片或富文本 post；Webhook 飞书签名仍使用 `webhook_secret`。

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
