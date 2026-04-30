"""Compatibility facade for AI-facing command manifests and guides."""

from __future__ import annotations

from codepilot.ai_support.agent_commands import _cmd, normalize_command_name, runtime_command_name
from codepilot.ai_support.agent_guides import ai_guide_markdown, ai_prompt_text
from codepilot.ai_support.agent_manifest import command_manifest, manifest_json
from codepilot.ai_support.agent_task_template import (
    TASK_TEMPLATE_PATH,
    _task_template_example_content,
    _task_template_markdown,
    task_template_guide_markdown,
    task_template_schema,
    task_template_schema_json,
)

__all__ = [
    "TASK_TEMPLATE_PATH",
    "_cmd",
    "_task_template_example_content",
    "_task_template_markdown",
    "ai_guide_markdown",
    "ai_prompt_text",
    "command_manifest",
    "manifest_json",
    "normalize_command_name",
    "runtime_command_name",
    "task_template_guide_markdown",
    "task_template_schema",
    "task_template_schema_json",
]
