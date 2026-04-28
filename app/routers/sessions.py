"""
Sessions router — managing exam session lifecycle.
POST /api/v1/sessions/start      — create a new active session
GET  /api/v1/sessions/{id}       — fetch session status
POST /api/v1/sessions/{id}/flag  — append a proctoring flag
POST /api/v1/sessions/{id}/analyze-frame — proxy a frame to the proctoring service
"""
import os
import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal
from datetime import datetime, timezone
from ..database import get_supabase

logger = logging.getLogger(__name__)

PROCTORING_SERVICE_URL: str = os.getenv("PROCTORING_SERVICE_URL", "http://localhost:5001")

router = APIRouter()


class StartSessionRequest(BaseModel):
    exam_id: str
    student_id: str
    photo_verification_url: str | None = None


class FlagRequest(BaseModel):
    flag_type: Literal[
        "tab_switch", "copy_paste", "multiple_faces", "no_face",
        "unknown_face", "head_movement", "phone_detected",
        "electronic_device", "unusual_eye_movement", "other"
    ]
    severity: Literal["low", "medium", "high"]
    description: str
    frame_url: str | None = None


class AnalyzeFrameRequest(BaseModel):
    frame_b64: str
    tab_switches: int = 0
    copy_paste_attempts: int = 0


@router.post("/start")
async def start_session(req: StartSessionRequest):
    """Create and return a new exam session."""
    sb = get_supabase()

    # Ensure the exam exists and is not already completed by this student
    existing = (
        sb.table("exam_sessions")
        .select("id, status")
        .eq("exam_id", req.exam_id)
        .eq("student_id", req.student_id)
        .execute()
    )
    for s in (existing.data or []):
        if s["status"] in ("active", "completed"):
            raise HTTPException(
                status_code=409,
                detail=f"Session already {s['status']}. ID: {s['id']}"
            )

    resp = sb.table("exam_sessions").insert({
        "exam_id": req.exam_id,
        "student_id": req.student_id,
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "photo_verification_url": req.photo_verification_url,
        "proctoring_flags": [],
    }).execute()

    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to create session")

    session = resp.data[0]

    # Register the student's reference face with the proctoring service so
    # that subsequent /analyze-frame calls can perform identity verification.
    if req.photo_verification_url:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    f"{PROCTORING_SERVICE_URL}/register-face",
                    json={
                        "session_id": session["id"],
                        "photo_url": req.photo_verification_url,
                    },
                )
        except Exception as exc:
            # Non-fatal: proctoring service may not be running during tests
            logger.warning("Could not register face with proctoring service: %s", exc)

    return session


@router.get("/{session_id}")
async def get_session(session_id: str):
    sb = get_supabase()
    resp = sb.table("exam_sessions").select("*").eq("id", session_id).single().execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Session not found")
    return resp.data


@router.post("/{session_id}/flag")
async def add_flag(session_id: str, req: FlagRequest):
    """Append a proctoring incident flag to a session."""
    sb = get_supabase()

    # Fetch current flags
    resp = sb.table("exam_sessions").select("proctoring_flags").eq("id", session_id).single().execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Session not found")

    flags = resp.data.get("proctoring_flags") or []
    flags.append({
        "type": req.flag_type,
        "severity": req.severity,
        "description": req.description,
        "frame_url": req.frame_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    sb.table("exam_sessions").update({"proctoring_flags": flags}).eq("id", session_id).execute()
    return {"ok": True, "flag_count": len(flags)}


@router.post("/{session_id}/analyze-frame")
async def analyze_frame(session_id: str, req: AnalyzeFrameRequest):
    """
    Forward a webcam frame to the proctoring microservice, persist any
    detected violations as flags on the session, and return the analysis.

        Body:
            {
                "frame_b64": "<base64 JPEG>",
                "tab_switches": 0,
                "copy_paste_attempts": 0
            }
    """
    sb = get_supabase()

    # Verify session exists and is active
    session_resp = (
        sb.table("exam_sessions")
        .select("id, status, proctoring_flags")
        .eq("id", session_id)
        .single()
        .execute()
    )
    if not session_resp.data:
        raise HTTPException(status_code=404, detail="Session not found")
    if session_resp.data.get("status") != "active":
        raise HTTPException(status_code=409, detail="Session is not active")

    # Forward to proctoring service
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            proctor_resp = await client.post(
                f"{PROCTORING_SERVICE_URL}/analyze-frame",
                json={
                    "frame_b64": req.frame_b64,
                    "session_id": session_id,
                    "tab_switches": req.tab_switches,
                    "copy_paste_attempts": req.copy_paste_attempts,
                },
            )
            proctor_resp.raise_for_status()
            analysis = proctor_resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Proctoring service error: {exc}")

    # Persist each new flag back into Supabase
    new_flags = analysis.get("flags", [])
    if new_flags:
        existing_flags = session_resp.data.get("proctoring_flags") or []
        now = datetime.now(timezone.utc).isoformat()
        for f in new_flags:
            existing_flags.append({
                "type": f.get("type", "other"),
                "severity": f.get("severity", "medium"),
                "description": f.get("description", ""),
                "frame_url": None,
                "timestamp": now,
            })
        sb.table("exam_sessions").update(
            {"proctoring_flags": existing_flags}
        ).eq("id", session_id).execute()

    return analysis
