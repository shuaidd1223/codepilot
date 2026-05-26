"""Structured reviewer output parser tests.

Covers the JSON-first / legacy-fallback hybrid introduced alongside the
`reviewer_rules.md` machine-readable verdict block. The parser must stay
backward compatible with transcripts that only carry the legacy
``VERDICT: PASS|FAIL`` + ``需要修复的点`` shape.
"""

from __future__ import annotations

from codepilot.commands.reviewer_output import (
    ReviewerVerdict,
    format_findings_for_builder,
    parse_reviewer_output,
)


def test_parse_json_pass_without_prose():
    raw = """```json
{"verdict": "pass", "ac_checks": [{"id": "AC-1", "status": "PASS"}], "blockers": [], "advisory": []}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "pass"
    assert result.source == "json"
    assert result.blockers == []
    assert result.advisory == []
    assert result.ac_checks == [{"id": "AC-1", "status": "PASS", "reason": ""}]


def test_parse_json_fail_with_blockers_and_advisory():
    raw = """AC #1: FAIL
需要修复的点:
- 旧的中文摘要

VERDICT: FAIL
```json
{
  "verdict": "fail",
  "ac_checks": [{"id": "AC-1", "status": "FAIL", "reason": "没有实现 --json"}],
  "blockers": ["给 status 加一个 --json 分支，按 json.dumps 输出"],
  "advisory": ["status.py 有旧死代码，非本任务范围"]
}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "json"
    assert result.blockers == ["给 status 加一个 --json 分支，按 json.dumps 输出"]
    assert result.advisory == ["status.py 有旧死代码，非本任务范围"]
    assert result.ac_checks[0]["status"] == "FAIL"
    assert result.raw_json is not None


def test_parse_json_pass_with_blockers_is_contradictory_fail():
    raw = """Reviewer prose accidentally says this is okay.

VERDICT: PASS
```json
{
  "verdict": "pass",
  "ac_checks": [{"id": "AC-1", "status": "PASS", "reason": "mostly ok"}],
  "blockers": ["补上缺失的回归测试"],
  "advisory": []
}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "json"
    assert result.blockers == ["补上缺失的回归测试"]


def test_parse_json_pass_with_failed_ac_is_contradictory_fail():
    raw = """VERDICT: PASS
```json
{
  "verdict": "pass",
  "ac_checks": [{"id": "AC-2", "status": "FAIL", "reason": "没有覆盖边界场景"}],
  "blockers": [],
  "advisory": []
}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "json"
    assert result.blockers == ["AC-2: 没有覆盖边界场景"]


def test_parse_json_fail_without_blockers_falls_back_to_needs_fix_prose():
    """verdict=fail in JSON but blockers empty → scavenge 需要修复的点 block."""
    raw = """AC #1: FAIL (挂了)
需要修复的点:
- 补上缺失的 --json 分支

VERDICT: FAIL
```json
{"verdict": "fail", "blockers": []}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "json"
    assert result.blockers == ["- 补上缺失的 --json 分支"]


def test_parse_malformed_json_falls_back_to_legacy_regex():
    """Bad JSON body inside the fence must not crash; legacy path takes over."""
    raw = """AC #1: FAIL
需要修复的点:
- 处理超时

VERDICT: FAIL
```json
{"verdict": "fail", "blockers": [oops this is not valid JSON]
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "legacy"
    assert result.blockers == ["- 处理超时"]


def test_parse_missing_verdict_field_in_json_falls_back_to_legacy():
    raw = """VERDICT: PASS
```json
{"ac_checks": [], "blockers": []}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "pass"
    assert result.source == "legacy"


def test_parse_unknown_verdict_value_in_json_falls_back_to_legacy():
    raw = """VERDICT: FAIL
```json
{"verdict": "maybe"}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "legacy"


def test_parse_legacy_only_pass_transcript():
    raw = """AC #1: PASS (tests green)
AC #2: PASS (shows up in status.py:45)
VERDICT: PASS
"""
    result = parse_reviewer_output(raw)
    assert result.verdict == "pass"
    assert result.source == "legacy"
    assert result.blockers == []


def test_parse_legacy_only_fail_with_needs_fix_section():
    raw = """AC #1: PASS
AC #2: FAIL (没看到 --json 分支)
需要修复的点:
- status.py 里加一个 --json 分支
VERDICT: FAIL
"""
    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "legacy"
    assert result.blockers == ["- status.py 里加一个 --json 分支"]


def test_parse_no_verdict_priority_review_findings_do_not_default_to_pass():
    raw = """The patch introduces regressions that should be addressed before accepting.

Full review comments:

- [P1] Guard old runtime cleanup against shared slugs — codepilot/storage/database.py:686
  When two registered project names slugify to the same runtime directory, renaming one can delete the shared root.

- [P2] Tolerate orphaned log paths during rename — codepilot/storage/database.py:738
  Missing historical log files should not abort rename.
"""
    result = parse_reviewer_output(raw)

    assert result.verdict == "fail"
    assert result.source == "legacy"
    assert any("Guard old runtime cleanup" in item for item in result.blockers)
    assert any("Tolerate orphaned log paths" in item for item in result.blockers)


def test_parse_no_verdict_p3_review_finding_is_unknown_not_pass():
    raw = """Review comments:

- [P3] Use normalized rename results in the client — codepilot/web/boundaries/AppSubmissionBoundary.js:97
  Use the returned normalized name after rename.
"""
    result = parse_reviewer_output(raw)

    assert result.verdict == "unknown"
    assert result.source == "legacy"
    assert result.blockers == [
        "[P3] Use normalized rename results in the client — codepilot/web/boundaries/AppSubmissionBoundary.js:97\n"
        "Use the returned normalized name after rename."
    ]


def test_parse_legacy_pass_with_blocking_findings_is_contradictory_fail():
    raw = """Review comments:

- [P1] Missing regression coverage — tests/test_project_rename.py:10
  The new path is untested.

VERDICT: PASS
"""
    result = parse_reviewer_output(raw)

    assert result.verdict == "fail"
    assert result.source == "legacy"
    assert result.blockers


def test_parse_empty_output_is_unknown():
    result = parse_reviewer_output("")
    assert result.verdict == "unknown"
    assert result.source == "empty"
    assert result.blockers == []


def test_parse_multiple_json_fences_uses_last_valid_object():
    raw = """First attempt with partial analysis:
```json
{"verdict": "pass"}
```

Updated verdict after re-check:
```json
{"verdict": "fail", "blockers": ["重做这个"]}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "fail"
    assert result.source == "json"
    assert result.blockers == ["重做这个"]


def test_parse_json_fence_without_language_tag_still_works():
    raw = """```
{"verdict": "pass", "ac_checks": [], "blockers": [], "advisory": []}
```"""

    result = parse_reviewer_output(raw)
    assert result.verdict == "pass"
    assert result.source == "json"


def test_format_findings_handles_existing_bullet_prefix():
    model = ReviewerVerdict(
        verdict="fail",
        blockers=["- 已经带 dash", "没带 dash", "  "],
    )
    out = format_findings_for_builder(model)
    assert out == "- 已经带 dash\n- 没带 dash"


def test_format_findings_empty_for_pass_verdict():
    model = ReviewerVerdict(verdict="pass", blockers=[])
    assert format_findings_for_builder(model) == ""
