from __future__ import annotations

from pathlib import Path


def test_task_detail_component_keeps_single_computed_block():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert source.count("computed:") == 1
    assert "task() { return this.s.taskDetail; }" in source
    assert "canSplit()" in source


def test_task_detail_keeps_single_live_log_panel():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert "实时日志" in source
    assert "实时进度" not in source
    assert "<cp-live-log" not in source
