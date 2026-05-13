"""Shared schema helpers for structured clarification questions/answers."""

from __future__ import annotations

import re
from typing import Any, Optional


QUESTION_TYPES = {"text", "single", "multi"}
_SPLIT_RE = re.compile(r"[,，/、\n]+")


def normalize_text(text: Any) -> str:
    return " ".join(str(text or "").split())


def _fallback_question_id(index: int) -> str:
    return f"q{int(index)}"


def _fallback_option_id(index: int) -> str:
    return f"opt{int(index)}"


def normalize_clarification_option(option: Any, index: int) -> Optional[dict]:
    if isinstance(option, str):
        label = normalize_text(option)
        if not label:
            return None
        return {"id": _fallback_option_id(index), "label": label}
    if not isinstance(option, dict):
        return None
    label = normalize_text(option.get("label") or option.get("text") or option.get("value"))
    if not label:
        return None
    option_id = normalize_text(option.get("id") or option.get("value") or _fallback_option_id(index))
    return {
        "id": option_id or _fallback_option_id(index),
        "label": label,
    }


def normalize_clarification_question(question: Any, index: int) -> Optional[dict]:
    if isinstance(question, str):
        text = normalize_text(question)
        if not text:
            return None
        return {
            "id": _fallback_question_id(index),
            "type": "text",
            "text": text,
            "options": [],
            "allow_free_text": False,
        }
    if not isinstance(question, dict):
        return None
    text = normalize_text(question.get("text") or question.get("question") or question.get("label"))
    if not text:
        return None
    qtype = normalize_text(question.get("type") or "text").lower()
    if qtype not in QUESTION_TYPES:
        qtype = "text"
    options: list = []
    for idx, raw_item in enumerate(question.get("options") or [], 1):
        normalized = normalize_clarification_option(raw_item, idx)
        if normalized is not None:
            options.append(normalized)
    if qtype in {"single", "multi"} and not options:
        qtype = "text"
    allow_free_text = bool(question.get("allow_free_text", qtype in {"single", "multi"}))
    if qtype == "text":
        allow_free_text = False
    question_id = normalize_text(
        question.get("id") or question.get("question_id") or _fallback_question_id(index)
    )
    return {
        "id": question_id or _fallback_question_id(index),
        "type": qtype,
        "text": text,
        "options": options,
        "allow_free_text": allow_free_text,
    }


def normalize_clarification_questions(questions: Optional[list[Any]]) -> list[dict]:
    rows: list[dict] = []
    for idx, item in enumerate(questions or [], 1):
        normalized = normalize_clarification_question(item, idx)
        if normalized is None:
            continue
        rows.append(normalized)
    return rows


def question_text(question: Any) -> str:
    if isinstance(question, dict):
        return normalize_text(question.get("text"))
    return normalize_text(question)


def _option_map(question: dict) -> dict[str, dict]:
    return {
        normalize_text(option.get("id")): option
        for option in (question.get("options") or [])
        if isinstance(option, dict) and normalize_text(option.get("id"))
    }


def _tokenize_choice_input(raw_text: str) -> list[str]:
    return [normalize_text(part) for part in _SPLIT_RE.split(raw_text or "") if normalize_text(part)]


def _match_choice_tokens(question: dict, raw_text: str) -> tuple[list[str], str]:
    tokens = _tokenize_choice_input(raw_text)
    if not tokens:
        return [], ""
    options = list(question.get("options") or [])
    option_map = _option_map(question)
    matched_ids: list[str] = []
    unmatched: list[str] = []
    for token in tokens:
        matched_id = ""
        if token.isdigit():
            pos = int(token)
            if 1 <= pos <= len(options):
                matched_id = normalize_text(options[pos - 1].get("id"))
        if not matched_id:
            lower = token.lower()
            for option in options:
                option_id = normalize_text(option.get("id"))
                option_label = normalize_text(option.get("label"))
                if lower in {option_id.lower(), option_label.lower()}:
                    matched_id = option_id
                    break
        if matched_id and matched_id in option_map and matched_id not in matched_ids:
            matched_ids.append(matched_id)
            continue
        unmatched.append(token)
    return matched_ids, normalize_text(" ".join(unmatched))


def _answer_item_lookup(raw_answers: Optional[list[Any]]) -> tuple[dict[str, dict], list[dict], bool]:
    answers_by_qid: dict[str, dict] = {}
    answers_in_order: list[dict] = []
    allow_positional_lookup = True
    for item in raw_answers or []:
        if not isinstance(item, dict):
            continue
        answers_in_order.append(item)
        qid = normalize_text(item.get("question_id") or item.get("id"))
        if qid:
            allow_positional_lookup = False
            answers_by_qid[qid] = item
    return answers_by_qid, answers_in_order, allow_positional_lookup


def _single_question_answer_item(question: dict, raw_answer_text: str) -> Optional[dict]:
    if not raw_answer_text:
        return None
    if question["type"] == "text":
        return {"free_text": raw_answer_text}
    selected, free_text = _match_choice_tokens(question, raw_answer_text)
    return {"selected_option_ids": selected, "free_text": free_text}


def _extract_answer_free_text(item: dict) -> str:
    return normalize_text(
        item.get("free_text") or item.get("text") or item.get("answer") or item.get("value")
    )


def _extract_answer_selected_ids(item: dict) -> list[str]:
    raw_selected = item.get("selected_option_ids") or item.get("selected_ids") or []
    if isinstance(raw_selected, str):
        raw_selected = _tokenize_choice_input(raw_selected)
    if not isinstance(raw_selected, list):
        raw_selected = []
    return [normalize_text(value) for value in raw_selected if normalize_text(value)]


def _normalize_answer_payload(question: dict, item: dict) -> tuple[list[str], str]:
    free_text = _extract_answer_free_text(item)
    selected_option_ids = _extract_answer_selected_ids(item)
    if question["type"] in {"single", "multi"} and not selected_option_ids and free_text:
        matched_ids, unmatched = _match_choice_tokens(question, free_text)
        if matched_ids:
            selected_option_ids = matched_ids
            free_text = unmatched
    if question["type"] == "single":
        selected_option_ids = selected_option_ids[:1]
        if question.get("allow_free_text") and free_text:
            selected_option_ids = []
    return selected_option_ids, free_text


def build_clarification_answer_entry(
    question: dict,
    *,
    selected_option_ids: Optional[list[str]] = None,
    free_text: str = "",
) -> Optional[dict]:
    normalized_question = normalize_clarification_question(question, 1)
    if normalized_question is None:
        return None
    qtype = normalized_question["type"]
    free_text = normalize_text(free_text)
    option_map = _option_map(normalized_question)
    ordered_labels: list[str] = []
    ordered_ids: list[str] = []
    for option in normalized_question.get("options") or []:
        option_id = normalize_text(option.get("id"))
        if option_id and option_id in set(selected_option_ids or []):
            ordered_ids.append(option_id)
            ordered_labels.append(normalize_text(option.get("label")))
            if qtype == "single":
                break
    if qtype == "text":
        answer_text = free_text
        ordered_ids = []
        ordered_labels = []
    else:
        parts = []
        if ordered_labels:
            parts.append("、".join(ordered_labels))
        if free_text:
            parts.append(f"其他：{free_text}")
        answer_text = " / ".join(part for part in parts if part)
    if not answer_text:
        return None
    return {
        "question_id": normalized_question["id"],
        "question_text": normalized_question["text"],
        "type": qtype,
        "selected_option_ids": ordered_ids,
        "selected_option_labels": ordered_labels,
        "free_text": free_text if qtype != "text" else answer_text,
        "answer_text": answer_text,
    }


def normalize_clarification_answers(
    questions: Optional[list[Any]],
    raw_answers: Optional[list[Any]] = None,
    answer_text: str = "",
) -> list[dict]:
    normalized_questions = normalize_clarification_questions(questions)
    if not normalized_questions:
        return []

    rows: list[dict] = []
    answers_by_qid, answers_in_order, allow_positional_lookup = _answer_item_lookup(raw_answers)
    raw_answer_text = normalize_text(answer_text)
    question_count = len(normalized_questions)
    for idx, question in enumerate(normalized_questions):
        item = answers_by_qid.get(question["id"])
        if item is None and allow_positional_lookup and idx < len(answers_in_order):
            item = answers_in_order[idx]
        if item is None:
            item = _single_question_answer_item(question, raw_answer_text) if question_count == 1 else None
            if item is None:
                continue

        selected_option_ids, free_text = _normalize_answer_payload(question, item)
        row = build_clarification_answer_entry(
            question,
            selected_option_ids=selected_option_ids,
            free_text=free_text,
        )
        if row is not None:
            rows.append(row)
    return rows


def build_clarification_answer_summary(answer_entries: Optional[list[Any]]) -> str:
    parts: list[str] = []
    for item in answer_entries or []:
        if not isinstance(item, dict):
            continue
        question = normalize_text(item.get("question_text"))
        answer = normalize_text(item.get("answer_text"))
        if not answer:
            continue
        if question:
            parts.append(f"{question}：{answer}")
        else:
            parts.append(answer)
    return " / ".join(parts)


def build_clarification_input_summary(
    questions: Optional[list[Any]] = None,
    *,
    raw_answers: Optional[list[Any]] = None,
    answer_text: str = "",
) -> str:
    normalized_questions = normalize_clarification_questions(questions)
    if normalized_questions:
        normalized_answers = normalize_clarification_answers(
            normalized_questions,
            raw_answers=raw_answers,
            answer_text=answer_text,
        )
        summary = build_clarification_answer_summary(normalized_answers)
        if summary:
            return summary

    parts: list[str] = []
    for item in raw_answers or []:
        if not isinstance(item, dict):
            continue
        question = normalize_text(item.get("question_text") or item.get("question_id") or item.get("id"))
        raw_selected = item.get("selected_option_ids") or item.get("selected_ids") or []
        if isinstance(raw_selected, str):
            raw_selected = _tokenize_choice_input(raw_selected)
        if not isinstance(raw_selected, list):
            raw_selected = []
        selected = [normalize_text(value) for value in raw_selected if normalize_text(value)]
        free_text = normalize_text(
            item.get("free_text") or item.get("text") or item.get("answer") or item.get("value")
        )
        answer = " / ".join(part for part in ("、".join(selected), free_text) if part)
        if not answer:
            continue
        if question:
            parts.append(f"{question}：{answer}")
        else:
            parts.append(answer)
    return " / ".join(parts) or normalize_text(answer_text)


def normalize_clarification_history(qa_history: Optional[list[Any]]) -> list[dict]:
    rows: list[dict] = []
    for item in qa_history or []:
        if not isinstance(item, dict):
            continue
        questions = normalize_clarification_questions(item.get("questions"))
        if not questions and normalize_text(item.get("question")):
            questions = normalize_clarification_questions([item.get("question")])
        answers = normalize_clarification_answers(
            questions,
            raw_answers=item.get("answers") if isinstance(item.get("answers"), list) else [],
            answer_text=item.get("answer") or "",
        )
        summary = build_clarification_answer_summary(answers) or normalize_text(item.get("answer"))
        if not questions and not answers and not summary:
            continue
        rows.append({
            "questions": questions,
            "answers": answers,
            "answer": summary,
        })
    return rows


def render_clarification_question(question: Any, index: int, *, include_options: bool = True) -> str:
    normalized = normalize_clarification_question(question, index)
    if normalized is None:
        return ""
    lines = [f"{index}. {normalized['text']}"]
    if include_options and normalized["type"] in {"single", "multi"}:
        for opt_idx, option in enumerate(normalized.get("options") or [], 1):
            lines.append(f"   {opt_idx}. {option['label']}")
        if normalized.get("allow_free_text"):
            lines.append("   其他：可直接手动输入文本")
    return "\n".join(lines)


def render_clarification_questions(questions: Optional[list[Any]], *, include_options: bool = True) -> str:
    lines = [
        rendered
        for idx, question in enumerate(normalize_clarification_questions(questions), 1)
        if (rendered := render_clarification_question(question, idx, include_options=include_options))
    ]
    return "\n".join(lines)
