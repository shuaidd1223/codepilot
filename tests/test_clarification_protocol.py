from __future__ import annotations

from codepilot.ai_support.clarification_protocol import (
    build_clarification_answer_summary,
    build_clarification_input_summary,
    normalize_clarification_answers,
    normalize_clarification_history,
    normalize_clarification_questions,
)


def _q(
    text: str,
    *,
    qid: str = "q1",
    qtype: str = "text",
    options: list[tuple[str, str]] | None = None,
    allow_free_text: bool = False,
) -> dict:
    return {
        "id": qid,
        "type": qtype,
        "text": text,
        "options": [{"id": option_id, "label": label} for option_id, label in (options or [])],
        "allow_free_text": allow_free_text,
    }


def test_normalize_clarification_questions_coerces_invalid_choice_to_text():
    assert normalize_clarification_questions([
        "  说明目标  ",
        {
            "id": "scope",
            "type": "single",
            "text": "  先做哪块?  ",
            "options": [],
            "allow_free_text": True,
        },
    ]) == [
        _q("说明目标", qid="q1"),
        _q("先做哪块?", qid="scope"),
    ]


def test_normalize_clarification_answers_resolves_choice_labels_and_free_text_override():
    question = _q(
        "先覆盖哪个入口?",
        qid="entry",
        qtype="single",
        options=[("web", "Web UI"), ("cli", "CLI")],
        allow_free_text=True,
    )

    selected = normalize_clarification_answers(
        [question],
        raw_answers=[{"question_id": "entry", "selected_option_ids": ["web"]}],
    )
    custom = normalize_clarification_answers(
        [question],
        raw_answers=[{
            "question_id": "entry",
            "selected_option_ids": ["web"],
            "free_text": "桌面端",
        }],
    )

    assert selected[0]["answer_text"] == "Web UI"
    assert selected[0]["selected_option_labels"] == ["Web UI"]
    assert custom[0]["answer_text"] == "其他：桌面端"
    assert custom[0]["selected_option_ids"] == []


def test_normalize_clarification_answers_does_not_positionally_reuse_keyed_partial_answers():
    questions = [
        _q("先做哪块?", qid="scope"),
        _q("目标是什么?", qid="goal"),
    ]

    answers = normalize_clarification_answers(
        questions,
        raw_answers=[{"question_id": "goal", "free_text": "降低规划摩擦"}],
    )

    assert answers == [{
        "question_id": "goal",
        "question_text": "目标是什么?",
        "type": "text",
        "selected_option_ids": [],
        "selected_option_labels": [],
        "free_text": "降低规划摩擦",
        "answer_text": "降低规划摩擦",
    }]


def test_build_clarification_summaries_skip_empty_rows_and_accept_raw_input():
    question = _q(
        "覆盖范围?",
        qid="scope",
        qtype="multi",
        options=[("web", "Web UI"), ("cli", "CLI")],
        allow_free_text=True,
    )

    summary = build_clarification_input_summary(
        [question],
        raw_answers=[{"question_id": "scope", "selected_option_ids": ["web", "cli"]}],
    )

    assert summary == "覆盖范围?：Web UI、CLI"
    assert build_clarification_answer_summary([{}, {"answer_text": "  只保留这个  "}]) == "只保留这个"


def test_normalize_clarification_history_round_trips_structured_rows():
    question = _q("先做哪块?", qid="scope")

    history = normalize_clarification_history([
        {
            "questions": [question],
            "answers": [{
                "question_id": "scope",
                "free_text": "  Web UI  ",
            }],
            "answer": "ignored fallback",
        }
    ])

    assert history == [{
        "questions": [question],
        "answers": [{
            "question_id": "scope",
            "question_text": "先做哪块?",
            "type": "text",
            "selected_option_ids": [],
            "selected_option_labels": [],
            "free_text": "Web UI",
            "answer_text": "Web UI",
        }],
        "answer": "先做哪块?：Web UI",
    }]

