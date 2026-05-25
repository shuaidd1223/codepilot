"""Guards for the removed direct question-answering runtime."""

from __future__ import annotations

import importlib.util


def test_direct_question_answering_runtime_is_removed():
    assert importlib.util.find_spec("codepilot.ai_support.question_runtime") is None
    assert importlib.util.find_spec("codepilot.ai_support.question_answering") is None
