"""Prompt templates and JSON schemas used by planner / classifier.

The prose bodies of these templates now live in :mod:`codepilot.prompts`
(one ``.md`` file per template) so they can be tweaked without touching
Python. This module still owns the JSON schemas (which are data contracts
consumed both by the CLI ``--json-schema`` flag and by tests) and the
``AgentConfig`` dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codepilot.prompts import load_prompt as _load_prompt


class AgentConfig:
    """Agent runtime configuration."""
    builder: str = "codex"  # CLI name or API provider key
    reviewer: str = "codex"
    mode: str = "dual"  # "cli" | "api" | "dual"
    # API keys (can be overridden from environment variables)
    api_keys: dict[str, str] = field(default_factory=dict)

    def resolve_builder(self) -> tuple[str, str]:
        """Resolve builder and return (type, name)."""
        if self.builder in API_PROVIDERS:
            return "api", self.builder
        return "cli", self.builder

    def resolve_reviewer(self) -> tuple[str, str]:
        """Resolve reviewer and return (type, name)."""
        if self.reviewer in API_PROVIDERS:
            return "api", self.reviewer
        return "cli", self.reviewer




# Kept as module-level strings for backward compatibility with callers that
# imported them directly; callers that build their final prompt with
# `.format()` continue to work unchanged.
TASK_PROMPT_TEMPLATE = _load_prompt("task_single")


TASK_BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "complexity": {"type": "string", "enum": ["simple", "complex"]},
        "should_split": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "goal": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                    },
                    "builder_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reviewer_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "depends_on_indices": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "description": "Indices of prerequisite tasks in the tasks array; empty means no dependency.",
                    },
                    "risk_level": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Planner's risk estimate. 'high' if task touches auth/db/core dirs or introduces cross-module breakage risk.",
                    },
                    "scope_budget": {
                        "type": "string",
                        "description": "Rough scope envelope, e.g. '2 files / ~80 LOC' or 'single module'. Free-form but concise (English or numbers only).",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Short citation of the recon finding / existing file / open task / commit that justifies this task. Empty strings signal fabricated planning and will be flagged downstream.",
                    },
                },
                "required": [
                    "title",
                    "priority",
                    "goal",
                    "acceptance_criteria",
                    "builder_notes",
                    "reviewer_notes",
                    "files",
                    "notes",
                    "depends_on_indices",
                    "risk_level",
                    "scope_budget",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "complexity", "should_split", "tasks"],
    "additionalProperties": False,
}


RECON_SCHEMA = {
    "type": "object",
    "properties": {
        "current_state": {
            "type": "string",
            "description": "One-sentence summary of the current project state relevant to the requirement.",
        },
        "relevant_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Paths (relative to project root) that were read or are likely to change. Do not invent files.",
        },
        "key_findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key findings from code reading: existing implementation, related modules, likely blockers.",
        },
        "risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Risk points: breakage risks, weak test areas, or backward-compatibility concerns.",
        },
        "suggested_approach": {
            "type": "string",
            "description": "High-level implementation approach in one concise paragraph.",
        },
    },
    "required": ["current_state", "relevant_files", "key_findings", "suggested_approach"],
    "additionalProperties": False,
}


RECON_PROMPT_TEMPLATE = _load_prompt("task_recon")


TASK_BREAKDOWN_PROMPT_TEMPLATE = _load_prompt("task_breakdown")



