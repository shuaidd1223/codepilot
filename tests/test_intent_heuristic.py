"""Guards for the removed local intent classifier API."""

from __future__ import annotations

import importlib.util

from codepilot.ai_support import service


def test_local_intent_classifier_modules_are_removed_from_service_api():
    assert importlib.util.find_spec("codepilot.ai_support.classifier") is None
    assert importlib.util.find_spec("codepilot.ai_support.intent_classifier") is None
    assert not hasattr(service, "classify_intent")
    assert not hasattr(service, "answer_question_via_api")
    assert not hasattr(service, "_heuristic_intent")
