from __future__ import annotations

from codepilot.commands.status import _task_table


def test_task_table_keeps_dense_single_line_columns_in_default_view():
    table = _task_table(
        [
            {
                "id": 3,
                "priority": "P0",
                "agent": "dual",
                "title": "LLM 调用接入流式 heartbeat 推送 token 与 elapsed 到 progress_bus",
                "run_phase": "",
                "status": "backlog",
                "error_message": "内置执行器检测到主工作区已有未提交改动，已跳过执行。",
                "last_output": "",
                "delivery_record": "",
                "created_at": "2026-04-24 16:39:08",
            }
        ],
        verbose=False,
    )

    columns = {column.header: column for column in table.columns}
    assert columns["标题"].no_wrap is True
    assert columns["标题"].overflow == "ellipsis"
    assert columns["最近信息"].no_wrap is True
    assert columns["最近信息"].overflow == "ellipsis"


def test_task_table_keeps_dense_single_line_columns_in_verbose_view():
    table = _task_table(
        [
            {
                "id": 3,
                "priority": "P0",
                "agent": "dual",
                "title": "LLM 调用接入流式 heartbeat 推送 token 与 elapsed 到 progress_bus",
                "run_phase": "",
                "status": "backlog",
                "error_message": "内置执行器检测到主工作区已有未提交改动，已跳过执行。",
                "last_output": "很长很长的输出，需要在表格里单行截断显示。",
                "delivery_record": "",
                "created_at": "2026-04-24 16:39:08",
            }
        ],
        verbose=True,
    )

    columns = {column.header: column for column in table.columns}
    assert columns["创建时间"].no_wrap is True
    assert columns["最后输出"].no_wrap is True
    assert columns["最后输出"].overflow == "ellipsis"
