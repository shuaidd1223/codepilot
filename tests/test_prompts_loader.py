"""Prompt loader behaviour and file-backed template invariants."""

from __future__ import annotations

import pytest

from codepilot import prompts


def setup_function(_):
    """Each test starts with a clean cache — otherwise later cache_clear
    in other suites could make us observe stale data on retries."""
    prompts.clear_cache()


def test_load_prompt_returns_raw_when_no_kwargs():
    text = prompts.load_prompt("intent")
    # The intent template contains the placeholder verbatim, without
    # substitution having run.
    assert "{text}" in text


def test_load_prompt_substitutes_kwargs():
    text = prompts.load_prompt("intent", text="hello world")
    assert "hello world" in text
    assert "{text}" not in text


def test_load_prompt_preserves_missing_keys():
    text = prompts.load_prompt("clarify", title="only title supplied")
    assert "only title supplied" in text
    # context and history_block left as placeholders for later assembly.
    assert "{context}" in text
    assert "{history_block}" in text


def test_load_prompt_raises_for_unknown_name():
    with pytest.raises(prompts.PromptNotFoundError):
        prompts.load_prompt("this_prompt_does_not_exist")


def test_task_breakdown_template_has_json_examples_intact():
    # When called without kwargs, literal `{{` and `}}` in the JSON examples
    # must stay escaped so downstream `.format()` calls still work.
    raw = prompts.load_prompt("task_breakdown")
    assert '{{' in raw
    assert '}}' in raw


def test_task_breakdown_template_renders_for_full_substitution():
    out = prompts.load_prompt(
        "task_breakdown",
        max_tasks=5,
        title="demo",
        recon_block="(recon)",
        existing_tasks_block="(existing)",
        project_context="(ctx)",
    )
    assert "最多 5 个" in out
    # JSON example braces should now appear as single braces post-render.
    assert '"summary"' in out
    # Placeholders consumed.
    assert "{title}" not in out
    assert "{max_tasks}" not in out


def test_load_prompt_is_cached(tmp_path, monkeypatch):
    """Second call for the same name must not re-read the file — this is
    a performance guarantee for webui / auto-workflow hot paths."""
    reads: list[str] = []

    original_open = prompts._PROMPTS_DIR  # ensure module loaded

    from pathlib import Path

    real_read_text = Path.read_text

    def _tracking_read_text(self, *args, **kwargs):
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _tracking_read_text)
    prompts.clear_cache()

    prompts.load_prompt("intent")
    prompts.load_prompt("intent")
    prompts.load_prompt("intent")

    # Only the first call should have hit read_text for that file.
    intent_reads = [r for r in reads if r.endswith("intent.md")]
    assert len(intent_reads) == 1
