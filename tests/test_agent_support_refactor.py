from __future__ import annotations

import inspect
from pathlib import Path

from codepilot.ai_support import agent_support


def test_agent_support_is_thin_compatibility_facade():
    source_lines = Path(agent_support.__file__).read_text(encoding="utf-8").splitlines()

    assert len(source_lines) <= 80
    assert inspect.getmodule(agent_support.command_manifest).__name__ == "codepilot.ai_support.agent_manifest"
    assert inspect.getmodule(agent_support.ai_guide_markdown).__name__ == "codepilot.ai_support.agent_guides"
    assert inspect.getmodule(agent_support.ai_prompt_text).__name__ == "codepilot.ai_support.agent_guides"
    assert inspect.getmodule(agent_support.task_template_schema).__name__ == "codepilot.ai_support.agent_task_template"
