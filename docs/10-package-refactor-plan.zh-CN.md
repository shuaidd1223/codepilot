# CodePilot 目录重构计划

## 目标

将 `codepilot/` 根目录下原本平铺的 Python 模块按职责拆分，降低后续维护成本，并让导入路径与领域边界一致。

## 新目录结构

- `codepilot/core/`
  - 基础设施与跨域公共能力
  - 例如：`config.py`、`runtime.py`、`output.py`、`paths.py`
- `codepilot/storage/`
  - 数据库存取、会话、任务读模型
  - 例如：`database.py`、`config.py`、`project_store.py`
- `codepilot/ai_support/`
  - 任务规划、Provider、主 AI 流程
  - 例如：`service.py`、`planner_parse.py`、`providers.py`
- `codepilot/gateway/`
  - API/CLI 网关调用编排
  - 例如：`service.py`、`resolution.py`、`execute.py`
- `codepilot/binary_support/`
  - 二进制打包、版本、发布物
  - 例如：`manager.py`、`release.py`、`version.py`
- `codepilot/webapp/`
  - Web UI 服务端载荷、动作、Webhook、排序逻辑
  - 例如：`server.py`、`actions.py`、`payloads.py`

## 已完成

- 创建新的子包目录和 `__init__.py`
- 完成一轮文件物理迁移
- 保持 `commands/`、`prompts/`、`web/`、`templates/` 原有独立目录

## 待完成

1. 统一修正代码内导入路径到新包结构
2. 统一修正测试导入、`monkeypatch` 目标路径和模块文档字符串
3. 运行最小测试集，修补直接报错点
4. 运行完整测试集，清理残留路径引用
5. 评估是否需要继续细分 `commands/` 下的大文件

## 迁移原则

- 不保留旧的兼容壳模块
- 所有导入直接切换到新路径
- 先保证可导入，再逐步恢复测试通过
- 对外入口仅保留真正的入口文件：`codepilot/cli.py`、`codepilot/__main__.py`
