# 多项目任务执行与巡检服务改造说明

语言版本：中文 | [English](project-services.en-US.md)

本文记录“多个项目任务可同时执行、任务执行/巡检按项目独立控制，并在 Web UI 可视化操作”这一轮改造的完成情况、现状边界，以及后续待做和优化方向。

## 背景目标

原先 CodePilot 的任务执行更偏向单个全局 daemon：用户启动一次后台服务后，由它轮询任务队列。这个模型在多项目场景下不够直观，也不利于按项目控制任务执行和巡检。

本轮目标是：

- 多个项目的任务可以同时执行。
- 任务执行服务必须指定项目运行。
- 每个项目的任务执行服务可单独启动、停止、查看状态。
- 巡检按项目独立进程运行，可单独启动、停止、查看状态。
- Web UI 在项目页展示任务轮询和巡检状态，并提供开关操作。
- 停止任务轮询时，不中断当前正在执行的任务，只是不再领取下一个任务。

## 已完成

### 1. 任务执行 daemon 改为按项目独立服务

相关文件：

- `codepilot/commands/daemon.py`
- `codepilot/commands/webui_service.py`
- `codepilot/webui_actions.py`
- `codepilot/webui_payloads.py`

现在 `codepilot daemon` 后台运行时必须指定项目：

```powershell
codepilot daemon -p <项目名>
codepilot daemon -p <项目名> --status
codepilot daemon -p <项目名> --stop
```

每个项目有自己的服务状态目录：

```text
~/.codepilot/daemon/<project-slug>/
  daemon.lock
  daemon.heartbeat
  daemon.json
  daemon.log
  daemon.stop
```

这样不同项目的 daemon 互不抢同一个 PID/lock 文件，可以同时存在多个项目 daemon 进程。

### 2. 停止任务轮询改为优雅停止

任务执行服务的 `--stop` 和 Web UI 的“停止轮询”不会强杀 daemon 进程。

当前行为：

1. 写入 `daemon.stop` 停止请求文件。
2. daemon 当前正在执行的 `run_backlog(..., limit=1)` 会继续跑完。
3. 本轮任务结束后，daemon 检查到停止请求并退出。
4. 不再领取下一个任务。

这满足“已有任务在执行的时候不能关进程，只是下一个任务就不执行了”的要求。

### 3. 巡检服务改为按项目独立进程

相关文件：

- `codepilot/commands/inspect.py`
- `codepilot/webui_actions.py`
- `codepilot/webui_payloads.py`

现在巡检支持后台服务模式：

```powershell
codepilot inspect -p <项目名>
codepilot inspect -p <项目名> --status
codepilot inspect -p <项目名> --stop
codepilot inspect -p <项目名> --foreground
codepilot inspect -p <项目名> --once
```

每个项目有自己的巡检状态目录：

```text
~/.codepilot/inspect/<project-slug>/
  inspect.lock
  inspect.json
  inspect.log
```

### 4. Web UI 项目页增加项目服务控制

相关文件：

- `codepilot/web/components/ProjectView.js`
- `codepilot/web/app.js`
- `codepilot/web/styles.css`
- `codepilot/webui.py`
- `codepilot/webui_actions.py`
- `codepilot/webui_schema.py`

项目页新增“项目服务”区域，展示并控制：

- 任务执行服务
  - 运行状态
  - PID
  - 启动
  - 停止轮询
  - 停止中状态
- 项目巡检服务
  - 运行状态
  - PID
  - 启动
  - 停止

新增 Web UI API：

```text
POST /api/projects/<name>/tasks/start
POST /api/projects/<name>/tasks/stop
POST /api/projects/<name>/tasks/status

POST /api/projects/<name>/inspect/start
POST /api/projects/<name>/inspect/stop
POST /api/projects/<name>/inspect/status
```

### 5. Web UI 启动服务行为调整

`codepilot ui start` 仍然可以一键启动 Web UI。

如果传入项目，会同时启动该项目任务执行服务：

```powershell
codepilot ui start -p <项目名>
```

如果不传项目，只启动 Web UI，并提示可在项目页单独启动任务执行服务。

## 当前使用方式

### 启动某个项目任务轮询

```powershell
codepilot daemon -p demo
```

### 查看某个项目任务轮询状态

```powershell
codepilot daemon -p demo --status
```

### 停止某个项目任务轮询

```powershell
codepilot daemon -p demo --stop
```

该命令只请求停止轮询，不中断当前任务。

### 启动某个项目巡检

```powershell
codepilot inspect -p demo
```

### 查看巡检状态

```powershell
codepilot inspect -p demo --status
```

### 停止巡检

```powershell
codepilot inspect -p demo --stop
```

### 启动 Web UI 并同时启动某个项目任务轮询

```powershell
codepilot ui start -p demo
```

也可以只启动 Web UI，然后在项目页点击对应按钮。

## 已验证

新增或更新了以下测试覆盖：

- `tests/test_daemon_service.py`
  - daemon 按项目后台启动
  - daemon 按项目读取状态
  - daemon 停止时只写停止请求
  - daemon 启动必须指定项目
- `tests/test_inspect_service.py`
  - inspect 按项目后台启动
  - inspect 按项目读取状态
- `tests/test_webui_api.py`
  - Web UI 项目服务 API
  - 任务轮询停止请求语义
- `tests/test_webui_service.py`
  - Web UI 启动/重启时按项目启动任务服务
- `tests/test_webui_schema.py`
  - 项目服务状态字段纳入 Dashboard payload schema

全量测试已通过：

```text
360 passed
```

## 当前边界和已知问题

### 1. 巡检停止目前仍是强停止进程

任务执行服务已经是优雅停止：当前任务完成后退出。

巡检服务目前 `--stop` 仍然调用进程树停止逻辑。如果巡检正在等待 LLM、扫描项目或创建任务，可能会被中断。

后续建议把巡检也改成类似 daemon 的停止请求文件：

```text
inspect.stop
```

巡检循环在每轮结束后检查停止请求，再自然退出。

### 2. daemon 内仍保留旧的周期巡检入口

`codepilot/commands/daemon.py` 内部仍有 `_maybe_run_inspect(...)`，会根据项目配置触发巡检逻辑。

这用于兼容旧行为，但在“巡检是不同项目不同进程”的新模型下，存在潜在重复巡检风险：

- 如果项目配置启用了 inspect；
- 同时项目 daemon 在运行；
- 又单独启动了项目 inspect 服务；
- 则可能出现 daemon 内置巡检和独立巡检服务都在跑。

后续建议：

- 默认关闭 daemon 内置巡检；
- 或新增配置项区分 `daemon_embedded_inspect` 和 `inspect_service`；
- 或在启动独立 inspect 服务时，daemon 自动跳过该项目内置巡检。

### 3. Web UI 还没有暴露巡检细粒度配置

CLI 支持部分巡检参数：

```powershell
codepilot inspect -p demo --max 3 --planner claude --agent codex --interval 1800
```

Web UI 目前只提供启动、停止、状态，不提供：

- 巡检间隔配置
- 每轮最多新增任务数
- planner 选择
- agent 选择
- signals 选择
- dry-run 模式

后续可以在项目页增加“巡检设置”区域。

### 4. Web UI 还没有展示服务日志入口

状态 payload 已经包含日志路径：

- daemon log
- inspect log

但 Web UI 目前只展示状态和 PID，没有提供“查看日志”按钮或日志面板。

后续建议：

- 增加服务日志 API；
- 在项目页服务卡片上提供“查看日志”；
- 支持 tail 和自动刷新。

### 5. 服务状态主要依赖本地文件和 PID 检查

当前服务发现依赖：

- lock 文件
- meta JSON
- heartbeat 文件
- `is_process_alive(pid)`

这对本地单机使用足够直接，但还有优化空间：

- stale lock 自动清理策略更明确；
- Windows/Linux 下 PID 复用风险降低；
- meta 里记录启动命令、版本号、退出原因；
- 服务异常退出时在 Web UI 提示更具体。

### 6. 多项目并行的全局资源限制还不完善

现在多个项目可以通过多个项目 daemon 同时执行任务。

但系统层面的全局限流还没有完整设计，例如：

- 同一机器最多同时跑几个项目任务；
- 同一 agent/CLI 是否允许并发；
- API provider 限流；
- CPU/内存占用；
- 多项目同时 git 操作时的隔离提示。

后续可以加一个全局 scheduler 或全局 concurrency guard。

### 7. Web UI 服务健康横幅仍是全局摘要

`daemon_health_payload` 已兼容扫描项目级 daemon 心跳，避免项目 daemon 已运行但横幅显示未运行。

不过当前横幅仍是全局摘要，不是每个项目独立健康详情。项目级详情已经在项目页展示。

后续可考虑：

- 顶部横幅只提示当前项目服务状态；
- 或改成“有 N 个项目任务服务运行中，M 个停止中”。

## 待做清单

优先级建议如下。

### P0

- 将 daemon 内置周期巡检与独立 inspect 服务解耦，避免重复巡检。
- 巡检服务停止改成优雅停止，不直接杀进程。

### P1

- Web UI 增加巡检配置面板：interval、max_new、planner、agent、signals。
- Web UI 增加服务日志查看。
- Web UI 项目列表中展示服务状态小图标，不只在项目详情页展示。
- 增加“停止中”状态的自动刷新提示，daemon 自然退出后及时变为“未运行”。

### P2

- 增加全局并发限制配置，避免多项目同时执行时资源打满。
- 服务 meta 增加版本号、启动命令、退出原因。
- 增加服务异常退出后的 UI 诊断提示。
- 支持一键启动所有已启用项目的任务轮询或巡检服务。

### P3

- 增加服务历史记录，例如最近启动/停止时间、操作者、失败原因。
- 支持项目级默认服务策略：Web UI 启动时是否自动启动该项目任务轮询/巡检。
- 支持批量控制：批量启动、停止多个项目服务。

## 建议的后续实现顺序

1. 先处理重复巡检风险：决定 daemon 内置巡检是否默认关闭。
2. 把巡检停止改成和任务 daemon 一样的优雅停止。
3. Web UI 补巡检配置和日志查看。
4. 增加全局并发/资源限制。
5. 再做批量启动、批量停止、一键启动所有服务。
