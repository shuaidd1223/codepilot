"""Tests for lint signal grouping, fingerprint dedup, and noise reduction."""

from __future__ import annotations

from codepilot.commands.inspect_signals import (
    LINT_NOISE_CODES,
    lint_fingerprint,
    lint_group_key,
)
from codepilot.commands.inspect import _group_lint_candidates


def test_lint_group_key_returns_code_from_evidence():
    assert lint_group_key("foo.py F401 unused import") == "F401"
    assert lint_group_key("bar.py:5: F841 unused variable") == "F841"
    assert lint_group_key("F541 f-string") == "F541"
    assert lint_group_key("E402 module level import") == "E402"


def test_lint_group_key_returns_none_for_non_lint():
    assert lint_group_key("pytest collection error") is None
    assert lint_group_key("") is None
    assert lint_group_key("signal 5: failed tasks") is None


def test_lint_group_key_returns_first_match():
    assert lint_group_key("F401 and F841 in same ev") == "F401"


def test_lint_fingerprint_is_stable():
    assert lint_fingerprint("F401") == "inspect:ruff:F401"


def test_lint_fingerprint_differs_by_code():
    assert lint_fingerprint("F401") != lint_fingerprint("F841")


def test_lint_fingerprint_consistent():
    assert lint_fingerprint("F401") == lint_fingerprint("F401")


def test_noise_codes_contain_expected():
    for code in ("F401", "F841", "F541", "E402"):
        assert code in LINT_NOISE_CODES


def test_group_lint_candidates_merges_same_code():
    candidates = [
        {"title": "fix F401 foo", "goal": "\u6e05\u7406", "priority": "P3",
         "kind": "refactor", "evidence": "foo.py F401", "effort": "small"},
        {"title": "fix F401 bar", "goal": "\u6e05\u7406", "priority": "P3",
         "kind": "refactor", "evidence": "bar.py F401", "effort": "small"},
    ]
    grouped = _group_lint_candidates(candidates)
    assert len(grouped) == 1
    assert "F401" in grouped[0]["title"]


def test_group_lint_candidates_single_stays_unchanged():
    candidates = [
        {"title": "fix F401 foo", "goal": "\u6e05\u7406", "priority": "P3",
         "kind": "refactor", "evidence": "foo.py F401", "effort": "small"},
    ]
    grouped = _group_lint_candidates(candidates)
    assert len(grouped) == 1
    assert grouped[0]["title"] == "fix F401 foo"


def test_group_lint_candidates_keeps_high_priority():
    candidates = [
        {"title": "fix critical bug", "goal": "\u4fee\u590d", "priority": "P1",
         "kind": "bug", "evidence": "signal 5: pytest collect error", "effort": "medium"},
        {"title": "fix F401 foo", "goal": "\u6e05\u7406", "priority": "P3",
         "kind": "refactor", "evidence": "foo.py F401", "effort": "small"},
    ]
    grouped = _group_lint_candidates(candidates)
    assert len(grouped) == 2


def test_group_lint_candidates_preserves_non_lint():
    candidates = [
        {"title": "fix test", "goal": "\u4fee\u590d\u6d4b\u8bd5", "priority": "P2",
         "kind": "bug", "evidence": "signal 5: pytest error", "effort": "medium"},
    ]
    grouped = _group_lint_candidates(candidates)
    assert grouped == candidates


def test_group_lint_candidates_diff_codes_stay_separate():
    candidates = [
        {"title": "fix F401", "goal": "\u6e05\u7406", "priority": "P3",
         "kind": "refactor", "evidence": "foo.py F401", "effort": "small"},
        {"title": "fix F841", "goal": "\u6e05\u7406", "priority": "P3",
         "kind": "refactor", "evidence": "bar.py F841", "effort": "small"},
    ]
    grouped = _group_lint_candidates(candidates)
    assert len(grouped) == 2
