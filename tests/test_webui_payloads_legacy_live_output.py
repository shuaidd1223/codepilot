from __future__ import annotations

from textwrap import dedent

from codepilot.storage import database as db
from codepilot.webapp import task_payloads
from codepilot.webapp.live_output_payloads import normalize_legacy_live_output_markdown


def test_normalize_legacy_live_output_splits_runtime_role_and_exec_sections():
    raw = dedent(
        """
        ## Intro

        ## Live Output

        ~~~text
        booting runtime line
        user
        请实现 A
        assistant
        正在分析
        exec
        exec "rg -n foo" in D:\\repo
         succeeded in 12ms:
        foo
        ~~~
        ## Footer
        done
        """
    ).lstrip()

    normalized = normalize_legacy_live_output_markdown(raw)

    assert normalized == dedent(
        """
        ## Intro

        ## Live Output

        ### Runtime

        ~~~text
        booting runtime line
        ~~~

        ### User

        请实现 A
        ### Assistant

        正在分析
        ### Exec

        ~~~text
        exec "rg -n foo" in D:\\repo
         succeeded in 12ms:
        foo
        ~~~

        ## Footer
        done
        """
    ).lstrip()


def test_normalize_legacy_live_output_keeps_non_text_fence_unchanged():
    raw = dedent(
        """
        ## Live Output

        ~~~bash
        user
        echo "hello"
        ~~~
        """
    ).lstrip()

    assert normalize_legacy_live_output_markdown(raw) == raw


def test_normalize_legacy_live_output_keeps_plain_text_block_unchanged():
    raw = dedent(
        """
        ## Live Output

        ~~~text
        booting
        still booting
        ~~~
        """
    ).lstrip()

    assert normalize_legacy_live_output_markdown(raw) == raw


def test_normalize_legacy_live_output_handles_missing_closing_fence():
    raw = dedent(
        """
        ## Live Output

        ~~~text
        user
        hi
        exec
        echo hi
        """
    ).lstrip()

    normalized = normalize_legacy_live_output_markdown(raw)

    assert normalized == dedent(
        """
        ## Live Output

        ### User

        hi
        ### Exec

        ~~~text
        echo hi
        ~~~
        """
    ).lstrip()


def test_task_payload_includes_recovery_hints_on_timeout(tmp_path, monkeypatch):
    """_task_payload should include recovery_hints for builder_done_review_timeout tasks."""
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))
    task = db.create_task("demo", "timeout task", agent="dual", priority="P2")
    db.update_task(
        task["id"],
        status="backlog",
        error_message="✅ Builder 已完成但 ❌ Reviewer 超时。可以重试 review、切换 reviewer 或接受 builder 结果。",
    )
    refreshed = db.get_task(task["id"])
    payload = task_payloads._task_payload(refreshed)

    assert "recovery_hints" in payload
    assert isinstance(payload["recovery_hints"], list)
    if "超时" in (refreshed.get("error_message") or ""):
        assert len(payload["recovery_hints"]) > 0
        assert any("重试" in h or "review" in h.lower() for h in payload["recovery_hints"])


def test_recovery_hints_for_builder_done_review_timeout():
    """_derive_recovery_hints returns builder-specific hints for builder_done_review_timeout."""
    hints = task_payloads._derive_recovery_hints({
        "id": 99,
        "status": "failed",
        "error_message": "✅ Builder 已完成但 ❌ Reviewer 超时",
    })
    assert "Builder 已完成" in hints[0] or any("重试 review" in h for h in hints)
    assert any("接受 builder 结果" in h for h in hints)
    assert any("切换 reviewer" in h for h in hints)
    assert any("等待人工处理" in h for h in hints)

