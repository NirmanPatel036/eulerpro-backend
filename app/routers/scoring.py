"""
Scoring router — POST /api/v1/scoring/submit
Receives exam session answers, runs the scoring engine, writes results to Supabase.
"""
from fastapi import APIRouter, HTTPException
import logging
from pydantic import BaseModel
from typing import Any
from ..scoring_engine import score_exam
from ..database import get_supabase
from . import notifications

router = APIRouter()
logger = logging.getLogger(__name__)


class SubmitRequest(BaseModel):
    session_id: str
    exam_id: str
    answers: dict[str, Any]   # {question_id: answer}
    time_taken_seconds: int


class QuestionResult(BaseModel):
    question_id: str
    earned: float
    possible: float
    percentage: float
    is_correct: bool
    is_partial: bool


class SubmitResponse(BaseModel):
    session_id: str
    total_earned: float
    total_possible: float
    percentage: float
    passed: bool
    question_results: list[QuestionResult]


@router.post("/submit", response_model=SubmitResponse)
async def submit_exam(req: SubmitRequest):
    """Score a completed exam session and persist results."""
    sb = get_supabase()

    # 1 — Fetch questions for the exam
    exam_resp = sb.table("exams").select("passing_score").eq("id", req.exam_id).single().execute()
    if not exam_resp.data:
        raise HTTPException(status_code=404, detail="Exam not found")

    passing_score: float = exam_resp.data.get("passing_score", 60)

    questions_resp = (
        sb.table("questions")
        .select("*")
        .eq("exam_id", req.exam_id)
        .order("order")
        .execute()
    )
    questions = questions_resp.data or []

    if not questions:
        raise HTTPException(status_code=400, detail="Exam has no questions")

    # 2 — Score
    result = score_exam(questions, req.answers)
    passed = result["percentage"] >= passing_score

    # 3 — Update session in Supabase
    sb.table("exam_sessions").update({
        "status": "completed",
        "score": result["total_earned"],
        "max_score": result["total_possible"],
        "percentage": result["percentage"],
        "passed": passed,
        "time_taken_seconds": req.time_taken_seconds,
        "answers": req.answers,
        "question_results": result["question_results"],
    }).eq("id", req.session_id).execute()

    # Trigger result notification after successful final submit.
    try:
        notifications.send_result_email_for_submission(
            session_id=req.session_id,
            exam_id=req.exam_id,
            percentage=result["percentage"],
            passed=passed,
        )
    except Exception as exc:
        logger.warning("Result email trigger failed for session %s: %s", req.session_id, exc)

    return SubmitResponse(
        session_id=req.session_id,
        total_earned=result["total_earned"],
        total_possible=result["total_possible"],
        percentage=result["percentage"],
        passed=passed,
        question_results=[QuestionResult(**r) for r in result["question_results"]],
    )


@router.get("/session/{session_id}")
async def get_session_results(session_id: str):
    """Retrieve scored results for a session."""
    sb = get_supabase()
    resp = sb.table("exam_sessions").select("*").eq("id", session_id).single().execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Session not found")
    return resp.data
