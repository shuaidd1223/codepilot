from __future__ import annotations

from codepilot.commands.inspect_signal_collectors import collect_failed_tasks


def test_collect_failed_tasks_excludes_inspector_generated_failures(monkeypatch):
    monkeypatch.setattr(
        "codepilot.commands.inspect_signal_collectors.db.list_tasks",
        lambda **kwargs: [
            {
                "id": 1,
                "status": "failed",
                "title": "inspect 自己生成的失败任务",
                "source": "inspector",
                "error_message": "should be ignored",
            },
            {
                "id": 2,
                "status": "failed",
                "title": "用户任务失败",
                "source": "user",
                "error_message": "real error",
            },
        ],
    )

    result = collect_failed_tasks("demo")

    assert "#2 [failed] 用户任务失败  real error" in result
    assert "inspect 自己生成的失败任务" not in result


def test_collect_failed_tasks_returns_empty_when_only_inspector_failures(monkeypatch):
    monkeypatch.setattr(
        "codepilot.commands.inspect_signal_collectors.db.list_tasks",
        lambda **kwargs: [
            {
                "id": 1,
                "status": "failed",
                "title": "inspect 自己生成的失败任务",
                "source": "inspector",
                "error_message": "ignored",
            }
        ],
    )

    assert collect_failed_tasks("demo") == "（无）"
