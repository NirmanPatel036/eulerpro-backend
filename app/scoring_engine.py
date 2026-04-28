"""
Smart Scoring Engine — mirrors src/lib/scoring.ts on the backend side.
PRD formula:
  raw_score   = points_possible × difficulty_multiplier
  smart_score = (Σ earned) / (Σ raw) × 100
Difficulty multipliers: easy=0.8, medium=1.0, hard=1.5
Partial credit: 50% of raw for multi-select with ≥1 correct selection
Negative marking: deduct 25% of raw for completely wrong answers
"""
from __future__ import annotations
from typing import Any
import re

DIFFICULTY_MULT = {"easy": 0.8, "medium": 1.0, "hard": 1.5}


def _mult(difficulty: str) -> float:
    return DIFFICULTY_MULT.get(difficulty, 1.0)


def score_question(question: dict[str, Any], student_answer: Any) -> tuple[float, float]:
    """
    Returns (earned_points, possible_points).
    question dict shape matches the DB question JSON.
    """
    qtype = question["type"]
    pts = float(question.get("points", 1))
    diff = question.get("difficulty", "medium")
    m = _mult(diff)
    possible = pts * m
    partial = question.get("partial_credit", False)
    negative = question.get("negative_marking", False)

    earned = 0.0

    if qtype == "multiple_choice":
        correct = question.get("correct_option", 0)
        if student_answer == correct:
            earned = possible
        elif negative:
            earned = -possible * 0.25

    elif qtype == "checkbox":
        correct_set = set(question.get("correct_options", []))
        student_set = set(student_answer) if isinstance(student_answer, list) else set()
        if student_set == correct_set:
            earned = possible
        elif partial and student_set & correct_set:
            # At least one overlap — 50% credit
            earned = possible * 0.5

    elif qtype == "true_false":
        correct = question.get("correct_answer", True)
        if student_answer == correct:
            earned = possible
        elif negative:
            earned = -possible * 0.25

    elif qtype == "fill_blank":
        pattern = question.get("answer_regex", ".*")
        try:
            if isinstance(student_answer, str) and re.fullmatch(pattern, student_answer.strip(), re.IGNORECASE):
                earned = possible
        except re.error:
            # Bad regex — fall back to exact match
            if student_answer == question.get("sample_answer", ""):
                earned = possible

    elif qtype == "matching":
        pairs = question.get("pairs", [])
        if not pairs:
            return 0.0, possible
        # student_answer expected: {left: right} dict
        student_map = student_answer if isinstance(student_answer, dict) else {}
        correct_count = sum(
            1 for p in pairs if student_map.get(p["left"]) == p["right"]
        )
        ratio = correct_count / len(pairs)
        if ratio == 1.0:
            earned = possible
        elif partial and ratio > 0:
            earned = possible * 0.5

    elif qtype == "reorder":
        items = question.get("items", [])
        correct_order = question.get("correct_order", list(range(len(items))))
        if isinstance(student_answer, list) and student_answer == correct_order:
            earned = possible
        elif partial:
            if isinstance(student_answer, list):
                matches = sum(a == b for a, b in zip(student_answer, correct_order))
                if matches / max(len(correct_order), 1) >= 0.5:
                    earned = possible * 0.5

    return max(earned, 0.0), possible  # Never go below 0 for totals


def score_exam(
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
) -> dict[str, Any]:
    """
    Score an entire exam.
    Returns a result summary with per-question breakdown.
    """
    total_earned = 0.0
    total_possible = 0.0
    results = []

    for q in questions:
        qid = str(q["id"])
        answer = answers.get(qid)
        earned, possible = score_question(q, answer)
        total_earned += earned
        total_possible += possible
        results.append({
            "question_id": qid,
            "earned": round(earned, 2),
            "possible": round(possible, 2),
            "percentage": round((earned / possible * 100) if possible > 0 else 0, 1),
            "is_correct": earned >= possible,
            "is_partial": 0 < earned < possible,
        })

    pct = round((total_earned / total_possible * 100) if total_possible > 0 else 0, 1)
    return {
        "total_earned": round(total_earned, 2),
        "total_possible": round(total_possible, 2),
        "percentage": pct,
        "question_results": results,
    }
