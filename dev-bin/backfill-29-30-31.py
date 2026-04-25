"""Backfill 模板合规 content 到 task #29/#30/#31。

之前提交时偷懒走了 codepilot add --no-ai，三条任务 content 全空，违反
我自己实现的 task-template 合规规范（#23）。这里用一次性脚本把它们
补成符合 ai template --format guide 输出的格式。

执行后再走 task edit / status 验证；脚本本身不会作为常驻工具。
"""

from __future__ import annotations

from codepilot import db
from codepilot.task_template import missing_task_template_sections


_BODY_29 = """\
# 修复 test_split_phase_helpers_preserve_runtime_phase_log_phase_and_review_bounds 预先失败测试

## Task Goal
让 tests/test_executor_feedback_loop.py::test_split_phase_helpers_preserve_runtime_phase_log_phase_and_review_bounds 在新的 progress_bus 事件 schema（Web SSE #21 引入 stage=display_label, extra.phase_kind, type=phase_start/phase_end）下重新通过。

## In Scope
- 修改测试断言读取 events 时按 phase_kind + type 过滤起始事件。
- stage 用 per-round 显示标签（"builder r2/3" / "reviewer r2/3"）断言。
- round / round_total 改成逐字段断言，避免与 phase_kind 等新字段做整体相等比较。

## Out of Scope
- 修改 progress_bus.emit / 任何 builder / reviewer 的实际实现。
- 重新设计事件 schema，保留 #21 引入的语义。

## Forbidden (Hard Boundary)
- 不要回退 #21 commit 的 stage / phase_kind / type 字段语义。
- 不要为了让测试过而在生产代码里加分支。

## Files In Scope
- tests/test_executor_feedback_loop.py

## Planning Evidence
- git show 52fe03f 对应 #21 commit 显示 progress_bus 字段变更。
- 现场跑测试报错 'builder r2/3' != 'builder' 与 events[:2] 取值越界，确认是 schema 漂移而非真正回归。
- run_builtin.py:745 / :815 emit 调用确认 stage=display_phase, extra.phase_kind, event_type 三参数同时存在。

## Risks & Notes
- 仅触碰一个测试用例，回归面只在 test_executor_feedback_loop。
- 全套测试必须重新跑一遍确认不会绕开新事件语义。

## Acceptance Criteria
- [x] tests/test_executor_feedback_loop.py 21/21 通过。
- [x] 全套 658/658 通过（之前的预先失败一并消除）。
- [x] 修改后断言能区分 phase_start / phase_end，不再依赖 events[:2] 顺序假设。

## Verification Matrix
| AC | 命令 | 预期 | 证据 |
| --- | --- | --- | --- |
| AC1 | `python -m pytest tests/test_executor_feedback_loop.py` | 21/21 pass | commit 809b0c5 后实测通过 |
| AC2 | `python -m pytest tests/` | 658/658 pass | 同上 |

## Reviewer Checkpoints
- 确认断言没有静默放宽（仍然检查 builder 在前 reviewer 在后、stage 含 r2/3 标签、phase_kind 标识规范名）。
- 确认未触碰 progress_bus 或 builder/reviewer 生产代码。
- 确认全量回归通过，无新 fail。

## Delivery Record
- Completed at — 2026-04-25
- Changed files — tests/test_executor_feedback_loop.py
- Verification result — 21/21 测试通过；全套 658/658
- Review verdict — `VERDICT: PASS`
- 提交 — 809b0c5
"""


_BODY_30 = """\
# 把 cli_renderer 扩到 go / auto / daemon 命令入口

## Task Goal
让 codepilot run / go / auto / daemon 任一 CLI 入口都能在执行期间订阅 progress_bus，并按 echo() 统一格式渲染 LLM heartbeat 与 phase 事件。

## In Scope
- 核查 commands/auto.py（go + auto 子命令）与 commands/daemon.py 的入口处是否包了 maybe_cli_renderer。
- 若已包则确认顺序正确（包在 run_backlog / run_requirement_workflow 调用外侧）。
- 若未包则补 with maybe_cli_renderer(): 块。

## Out of Scope
- 重新实现 cli_progress.cli_renderer 或 maybe_cli_renderer。
- 修改 progress_bus 订阅 / 发布机制。

## Forbidden (Hard Boundary)
- 不要在测试或 chat 等其他命令里强行接入 cli_renderer。
- 不要嵌套两层 cli_renderer（maybe_ 已用 ContextVar 做嵌套保护，不必再绕）。

## Files In Scope
- codepilot/commands/auto.py
- codepilot/commands/daemon.py
- codepilot/commands/run.py（已先行接入）

## Planning Evidence
- grep maybe_cli_renderer / cli_renderer 显示 auto.py:258/330 与 daemon.py:469/481 已经接入，由 #22 commit 976ed14 完成。
- run.py:425 在更早的 task #27 commit 9686b7b 已包好。
- ContextVar _CLI_RENDERER_ACTIVE 已防止嵌套重复订阅。

## Risks & Notes
- 由于已被并行 sub-session 完成，本任务实际是确认与登记，没有新代码改动。
- 如果以后新增 CLI 入口（例如 inspect 改成长流程），记得评估是否也要包 maybe_cli_renderer。

## Acceptance Criteria
- [x] go / auto / daemon 三个入口都在调用 run_requirement_workflow / run_backlog 之前包 maybe_cli_renderer。
- [x] 嵌套调用安全（ContextVar 检测 + 只 attach 一次）。
- [x] tests/test_cli_progress.py + tests/test_daemon_service.py 全过。

## Verification Matrix
| AC | 命令 | 预期 | 证据 |
| --- | --- | --- | --- |
| AC1 | `grep -n maybe_cli_renderer codepilot/commands/auto.py codepilot/commands/daemon.py` | auto.py:258/330, daemon.py:481 命中 | 实测 |
| AC2 | `python -m pytest tests/test_cli_progress.py tests/test_daemon_service.py` | 全过 | 12/12 pass |
| AC3 | `python -m pytest tests/` | 658/658 pass | 实测 |

## Reviewer Checkpoints
- 确认 maybe_cli_renderer 在所有 CLI 入口（不含 chat 等真正交互命令）都接入。
- 确认 ContextVar 嵌套保护测试存在并通过（test_maybe_cli_renderer_is_noop_when_nested）。
- 不需要新提交；如果有改动应说明原因。

## Delivery Record
- Completed at — 2026-04-25
- Changed files — 无（已由 commits 9686b7b / 976ed14 / 之前 sub-session 接入）
- Verification result — 三个入口均已接入；测试 12/12（cli_progress + daemon）+ 全套 658/658
- Review verdict — `VERDICT: PASS`
"""


_BODY_31 = """\
# replan 动作在 prompt 中引用 reviewer 结构化反馈

## Task Goal
让 _build_review_failure_triage_prompt 在传入结构化 reviewer verdict 时，把 blockers / advisory / ac_checks 显式地塞进 prompt 上下文，并在动作规则里要求 replan_content 必须引用至少一条具体 blocker，避免新任务凭空规划。

## In Scope
- 给 _build_review_failure_triage_prompt 增加 reviewer_verdict 形参（可选）。
- prompt 文本里追加 "reviewer 反馈结构化条目" 区块（blockers + advisory + ac_checks）。
- prompt 严格规则中追加：replan_content 必须明确指向 blocker；retry_hint 也尽量复述 blocker。
- _collect_review_failure_evidence 用 parse_reviewer_output 把 reviewer 文本解析后传过去。
- 新增单测覆盖结构化 verdict 注入。

## Out of Scope
- 改动 ReviewerVerdict 的字段或解析器。
- 改动 schema（仍是 5 个 action）。

## Forbidden (Hard Boundary)
- 不要在 reviewer_output 为空 / 解析失败时崩溃，必须 fallback 到当前的纯文本 review_summary 行为。
- 不要破坏现有 tests/test_workflow_backlog_triage 里 deterministic 用例。

## Files In Scope
- codepilot/commands/run_failure_triage.py
- tests/test_workflow_backlog_triage.py（新增/扩展）

## Planning Evidence
- run_failure_triage.py:112 现有 prompt 仅塞 _trim_triage_text(review_output, 500)，没有结构化 blockers。
- commands/reviewer_output.py:56 ReviewerVerdict dataclass 已有 blockers / advisory / ac_checks 字段。
- task #20 commit 9e7e0b4 已经在 progress_bus 推 review_verdict 结构化事件，本任务把同一结构延伸到 triage prompt。

## Risks & Notes
- 注入太多 reviewer 文本可能让 prompt 超长，需要对每条 blocker 限长（160 字内）。
- 解析器 source=empty 时不要把空列表强行写进 prompt（保持 fallback）。

## Acceptance Criteria
- [ ] _build_review_failure_triage_prompt 接受 reviewer_verdict 参数；为 None / source=empty 时输出与改动前一致。
- [ ] 当 verdict.blockers 非空时 prompt 中出现 "reviewer 阻塞项:" 区块，列出每条 blocker。
- [ ] prompt 严格规则中包含 replan_content 必须引用具体 blocker 的指令。
- [ ] _collect_review_failure_evidence 把 review_output 跑过 parse_reviewer_output 再注入。
- [ ] 新增单测验证 prompt 包含结构化 blockers + 严格规则。
- [ ] 全套测试通过。

## Verification Matrix
| AC | 命令 | 预期 | 证据 |
| --- | --- | --- | --- |
| AC1-2 | `python -m pytest tests/test_workflow_backlog_triage.py -k "structured"` | 新增 case pass | 待执行 |
| AC3 | grep "replan_content" prompt | 出现 blocker 引用要求 | 待执行 |
| AC6 | `python -m pytest tests/` | 全过 | 待执行 |

## Reviewer Checkpoints
- 确认 reviewer_verdict 为空时行为不变（grep _trim_triage_text(review_output 仍用）。
- 确认 prompt 长度受控（每条 blocker 限长）。
- 确认 source=json 与 source=legacy 两条路径都有覆盖。

## Delivery Record
- Completed at — 待填
- Changed files — 待填
- Verification result — 待填
- Review verdict — 待填
"""


def main() -> None:
    plan = {29: _BODY_29, 30: _BODY_30, 31: _BODY_31}
    for task_id, body in plan.items():
        missing = missing_task_template_sections(body)
        if missing:
            raise SystemExit(f"task #{task_id} body missing sections: {missing}")
        db.update_task(task_id, content=body)
        # #29 / #30 已经实现完，恢复 done; #31 还要做，回 backlog
        if task_id == 31:
            db.update_task(task_id, status="backlog")
        print(f"task #{task_id}: content backfilled ({len(body)} chars), template-compliant")


if __name__ == "__main__":
    main()
