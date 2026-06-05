"""File-backed prompt templates.

Historically every prompt was hard-coded inside whichever module used it
(``ai_prompts.py``, provider adapters, ``commands/run.py``). That made it
impossible to tune a prompt without editing and redeploying Python, and it
meant the same instruction block could drift across modules without anyone
noticing.

This package stores each prompt as a plain Markdown file so they can be
reviewed, diffed, and even A/B'd without touching code. The loader uses
``str.format_map`` so callers stay compatible with the previous
``.format(**kwargs)`` style — and we cache file reads so hot paths don't
stat the disk on every planner call.
"""

# Author: 帅呆呆 <2264505396@qq.com>
# Repository: https://gitee.com/shuai_dd/workflow
# License: MIT

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Mapping

from codepilot.core.config import normalize_agent_input_language
from codepilot.errors import PromptNotFoundError

_PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=128)
def _read_prompt_file(name: str, language: str = "en") -> str:
    """Return the raw text of ``codepilot/prompts/<name>.<language>.md``.

    The cache means production callers (webui, auto-workflow) pay at most
    one disk read per prompt per process. Tests that need to re-load after
    editing a file on disk can call :func:`clear_cache`.
    """
    lang = normalize_agent_input_language(language)
    path = _PROMPTS_DIR / f"{name}.{lang}.md"
    if not path.is_file():
        path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(
            f"prompt template '{name}.{lang}.md' or '{name}.md' not found under {_PROMPTS_DIR}"
        )
    return path.read_text(encoding="utf-8")


def load_prompt(name: str, /, *, language: str = "en", **kwargs: object) -> str:
    """Load ``name``'s Markdown template, optionally substituting kwargs.

    When called with no kwargs, returns the raw file contents verbatim —
    this is important because many prompt files contain literal ``{{`` /
    ``}}`` used by downstream callers' own ``.format(...)`` invocations
    (e.g. JSON example blocks). Running ``format_map`` would prematurely
    unescape those and break the caller's later substitution.

    When kwargs are supplied, we do the substitution here using
    ``format_map`` with a preserving view, so missing keys stay as
    ``{key}`` instead of raising ``KeyError``.
    """
    template = _read_prompt_file(name, normalize_agent_input_language(language))
    if not kwargs:
        return template
    return template.format_map(_PreservingFormat(kwargs))


class _PreservingFormat(dict):
    """Leaves `{key}` in place if the value isn't supplied, instead of KeyError."""

    def __init__(self, mapping: Mapping[str, object]):
        super().__init__(mapping)

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def clear_cache() -> None:
    """Forget previously-loaded templates. Primarily for tests."""
    _read_prompt_file.cache_clear()
