# CodePilot

CodePilot 是一个本地工程工作流 CLI，用来把自然语言需求转换成排队任务，并自动规划、执行、审查和交付。

## 项目定位

它主要解决三类问题：

1. 把一句需求变成任务
- 支持直接输入自然语言
- 自动判断是简单任务还是复杂任务
- 复杂任务会拆成线性子任务

2. 把任务真正执行掉
- 支持外部 `dispatch` 脚本
- 也支持内置执行器，直接调用 `codex exec`
- 每个任务执行后可以自动 review 和自动提交

3. 把执行过程管起来
- 任务状态写入 SQLite
- 支持实时日志、运行阶段、心跳、停止任务
- 卡住的任务可以自动回收，不会一直挂在 `in_progress`

## 当前能力

- 使用 `AGENTS.toml` 注册和管理项目
- 用 SQLite 保存任务、日志和运行状态
- 直接支持纯文本入口：`codepilot "你的需求"`
- 支持在规划时指定任务智能体，例如 `--agent codex` 或 `--agent claude`
- 支持会话模式：`codepilot chat` 或直接运行 `codepilot`
- 自动判断需求复杂度，并决定是否拆分
- 支持 `run / daemon` 执行 backlog
- 支持查看实时日志、停止任务、手动重试指定任务、自动回收卡住任务
- 支持打包成单文件二进制，并安装到系统命令路径
- 支持把现有二进制整理成标准发布目录
- 支持输出供其他 AI 直接调用的命令清单和使用手册

## 快速开始

初始化当前项目：

```bash
codepilot init .
```

直接输入一个需求：

```bash
codepilot "实现自动拆分和自动执行工作流"
```

进入持续交互模式：

```bash
codepilot chat
```

查看项目状态：

```bash
codepilot status -p <项目名> -v
```

精确查看单个任务详情：

```bash
codepilot show <task_id>
```

查看任务日志：

```bash
codepilot logs <task_id>
```

停止一个运行中的任务：

```bash
codepilot stop <task_id>
```

手动重试一个失败/取消的任务：

```bash
codepilot retry <task_id>
```

启动本地 Web UI：

```bash
codepilot ui
```

## 给其他 AI 用

如果你希望把 CodePilot 暴露给其他 AI、Agent 或自动化系统直接调用，当前已经内置了机器可读和 AI 友好的入口。

输出机器可读的命令清单：

```bash
codepilot ai manifest
```

输出 Markdown 调用手册：

```bash
codepilot ai guide
```

输出短提示词：

```bash
codepilot ai prompt
```

仓库根目录还提供了一份静态手册：

- `AI_MANIFEST.json`
- `AI_USAGE.zh-CN.md`

推荐做法：

1. 用 `codepilot ai manifest` 获取命令和工作流清单
2. 用 `codepilot status -p <项目名> --json` 拉结构化状态
3. 用 `codepilot show <task_id> --json` 精确拉取单个任务详情
4. 用 `codepilot "需求文本"` 直接把高层需求交给 CodePilot
5. 用 `codepilot logs <task_id>`、`codepilot stop <task_id>` 和 `codepilot retry <task_id>` 做运行期排障

`--json` 命令级输出契约（统一 envelope）：

```json
{
  "ok": true,
  "command": "status",
  "data": {}
}
```

失败场景会额外包含 `error`，例如 `{"error":{"message":"...","code":"..."}}`。

## Web UI

如果你觉得终端命令太重，可以直接起本地控制台：

```bash
codepilot ui
```

默认会打开：

```text
http://127.0.0.1:8766/
```

当前 Web UI 支持：

- 查看所有项目及其任务统计
- 查看单个项目的任务列表和当前运行状态
- 对任务执行 `重试 / 停止 / 插队`
- 自动刷新，方便盯运行中的任务

如果你不想自动打开浏览器：

```bash
codepilot ui --no-open
```

## 二进制打包

先安装构建依赖：

```bash
pip install .[build]
```

为当前系统构建单文件二进制：

```bash
codepilot binary build
```

构建完成后直接安装到用户命令目录：

```bash
codepilot binary build --install
```

安装已有二进制到系统命令路径：

```bash
codepilot binary install --binary ./dist/binary/linux-x86_64/codepilot
codepilot binary install --binary .\\dist\\binary\\windows-x86_64\\codepilot.exe
```

查看默认安装目录：

```bash
codepilot binary where
```

默认安装路径：

- Windows：`%LOCALAPPDATA%\\Programs\\CodePilot\\bin`
- Linux：`~/.local/bin`

说明：

- Windows 和 Linux 需要分别在各自系统上原生构建，不支持直接交叉打包
- `binary install` 会安装到用户目录，并在需要时自动写入用户 PATH

## 发布产物

把已经构建好的二进制整理成标准发布目录：

```bash
codepilot binary release
```

手动指定某个平台的二进制：

```bash
codepilot binary release --artifact windows-x86_64=dist/binary/windows-x86_64/codepilot.exe
codepilot binary release --artifact linux-x86_64=dist/binary/linux-x86_64/codepilot
```

发布前先自动构建当前平台：

```bash
codepilot binary release --build-current
```

校验最新一次发布目录：

```bash
codepilot binary verify
```

如果你想把“改版本号 + 构建当前平台 + 生成发布目录 + 校验”收成一个命令：

```bash
codepilot binary prepare --version 0.1.1
```

同样也可以直接使用顶层发布入口：

```bash
codepilot release prepare --version 0.1.1
codepilot release verify
codepilot release bundle --build-current
```

发布目录默认会生成到：

```text
dist/release/codepilot-<version>
```

其中包含：

- `release.json`：发布元数据
- `SHA256SUMS.txt`：二进制和压缩包校验值
- `<platform>/`：平台对应原始二进制和安装脚本
- `README.zh-CN.md`：中文安装说明
- `AI_USAGE.zh-CN.md`：给其他 AI 的调用手册
- `AI_MANIFEST.json`：机器可读的命令清单
- `SUMMARY.zh-CN.md`：发布摘要和交付建议
- Windows: `codepilot-<version>-<platform>.zip`
- Linux: `codepilot-<version>-<platform>.tar.gz`

`binary verify` 会检查：

- `release.json` 是否可解析
- `SHA256SUMS.txt` 是否存在且格式正确
- 发布目录中的二进制、压缩包、安装脚本是否齐全
- `README.zh-CN.md`、`AI_USAGE.zh-CN.md`、`AI_MANIFEST.json` 和 `SUMMARY.zh-CN.md` 是否存在
- 校验值是否和清单一致

`binary prepare` 会执行：

1. 同步更新 `pyproject.toml` 和 `codepilot/__init__.py` 的版本号
2. 构建当前平台二进制
3. 生成发布目录
4. 自动执行发布目录校验

## 配置说明

当前仓库的 `AGENTS.toml` 当前重点使用这些配置：

```toml
[project]
name = "demo"
default_mode = "dual"

[automation]
planner = "claude"
executor = "builtin"
auto_execute = true
auto_commit = true
max_review_rounds = 4

[inspect]
planner = "claude"
auto_execute = false

[agents]
builder = "codex"
reviewer = "claude"
codex_cmd = "codex"
claude_cmd = "claude"
```

关键点：

- `default_mode = "dual"` 表示走 builder/reviewer 双阶段执行；默认是 builder=codex、reviewer=claude，也可用 `[agents].builder / reviewer` 覆盖
- `planner = "claude"` 表示默认规划器使用 claude
- `[inspect].planner = "claude"` 表示默认巡检规划器使用 claude
- `max_review_rounds = 4` 给 reviewer ↔ builder 闭环足够迭代空间；reviewer 判 FAIL 时，意见会作为 `【上一轮 reviewer 的阻塞意见】` 注入下一轮 builder prompt，让它针对性修复而非重写
- `[agents]` 中的 `codex_cmd / claude_cmd` 会真正覆盖运行时 CLI 路径

## 当前仓库配置行为

- 当前仓库的 `AGENTS.toml` 把项目模式设为 `dual`（builder=codex，reviewer=claude）
- 当前仓库的 `AGENTS.toml` 把规划器设为 `claude`
- 当前仓库的 `AGENTS.toml` 把巡检规划器设为 `claude`
- 当前仓库的 `AGENTS.toml` 把 builder/reviewer 闭环轮数设为 4
- 运行时仍可按任务或命令动态指定其他智能体
- Codex 规划超时时，会自动降级成单任务继续执行
- 内置执行器会在执行前检查 Git 状态
- 如果工作区已脏、还没初始化 Git，或者 reviewer 无法正常运行，会提前给出自然语言提示

## 运行时控制

为了避免“任务看起来没反应，其实后台还在跑”的问题，现在运行态会额外保存：

- 当前阶段：`pending / builder / reviewer / dispatch`
- 最近一次心跳时间
- 活跃进程 PID
- 当前日志文件路径
- 最近一段输出
- 是否已收到停止请求

这意味着你现在可以：

- 用 `status -v` 看任务是否还活着
- 用 `logs` 看实时输出
- 用 `stop` 停掉当前任务
- 用 `retry` 把失败或取消的指定任务重新放回 backlog
- 让系统自动把失联且无进程的任务标记为 `failed`
