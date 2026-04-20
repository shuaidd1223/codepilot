from __future__ import annotations

from pathlib import Path


def test_task_detail_component_keeps_single_computed_block():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert source.count("computed:") == 1
    assert "task() { return this.s.taskDetail; }" in source
    assert "liveEvents()" in source
    assert "canSplit()" in source
