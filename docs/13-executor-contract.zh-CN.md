# Executor Contract 与 Fallback 遥测

本文档记录第一批 executor contract 的稳定字段。运行时仍沿用现有
`claude`、`codex`、`opencode` CLI family；本次只定义统一 contract、
fallback reason enum 和可查询遥测。

## Contract 阶段

每个 executor family 都暴露相同阶段：

1. `prepare`：检查模型、权限、工作区和环境注入。
2. `execute`：执行任务提示词，收集 stdout、stderr、退出码。
3. `observe`：提取 diff、日志、测试结果和文件触达范围。
4. `validate`：运行验证命令或解析执行器自带验证结果。
5. `review`：调用 reviewer 或本地规则产出 verdict。
6. `repair`：基于失败证据生成最小修复任务或重试提示。

## Family 映射

| Family | Provider key | Command env | Structured CLI | Text fallback | Builtin executor |
| --- | --- | --- | --- | --- | --- |
| `claude` | `claude` | `CODEPILOT_CLAUDE_CMD` | yes | yes | yes |
| `codex` | `codex` | `CODEPILOT_CODEX_CMD` | yes | yes | yes |
| `opencode` | `opencode` | `CODEPILOT_OPENCODE_CMD` | yes | yes | yes |

`aider` 仅作为 reserved executor contract 注册点保留，不进入
`CLI_FAMILIES`，也不参与默认 `fallback_cli_order`。

## Fallback Reason Enum

统一枚举定义在 `codepilot.ai_support.executor_contract.ExecutorFallbackReason`：

- `executor_unavailable`
- `timeout`
- `permission_denied`
- `authentication_required`
- `unsupported_model`
- `invocation_error`
- `none`

`invocation_error` 当前只保持 Codex 旧行为，用于识别 Windows 上 Codex
启动失败时可能出现的 `[Errno 22] Invalid argument`。

## 遥测字段

builtin executor 发生工具 fallback 时，会在失败阶段任务日志中追加一行：

```text
CODEPILOT_EXECUTOR_TELEMETRY: {"kind":"executor_fallback", ...}
```

`codepilot trace --json` 会从该标记展开以下字段，便于查询：

- `executor_family`
- `executor_model`
- `fallback_reason`
- `fallback_path`
- `failed_executor`
- `fallback_executor`

