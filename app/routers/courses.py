"""
Courses router — course management, CSV enrollment sync, exam publish blast.

POST   /api/v1/courses/                          Create a course
GET    /api/v1/courses/                          List instructor's courses
GET    /api/v1/courses/{course_id}               Course detail
PATCH  /api/v1/courses/{course_id}               Update course (name/cover)
DELETE /api/v1/courses/{course_id}               Delete course

POST   /api/v1/courses/{course_id}/enroll-csv    Upload CSV → sync enrollments
GET    /api/v1/courses/{course_id}/roster        Get enrolled students
DELETE /api/v1/courses/{course_id}/roster/{id}   Remove one student

POST   /api/v1/courses/{course_id}/publish-exam  Email blast for a published exam
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import resend
from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, EmailStr
from ..__init__ import __file__ as _pkg  # noqa: F401 — used only for path anchor
from ..database import get_supabase
from ..config import settings
from . import notifications

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Shared email shell (mirrors notifications.py branding) ────────────────────

def _shell(header_label: str, header_icon: str, body_html: str) -> str:
        symbol_url = f"{settings.FRONTEND_URL.rstrip('/')}/symbol.png"
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{header_label} - EulerPro</title>
</head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:Inter,Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#f9fafb;padding:24px 12px;">
        <tr>
            <td align="center">
                <table width="600" cellpadding="0" cellspacing="0" role="presentation"
                             style="max-width:600px;width:100%;background:#ffffff;border-radius:20px;border:1px solid #e2e8f0;overflow:hidden;box-shadow:0 8px 30px rgba(15,23,42,0.05);">

                    <tr>
                        <td align="center" style="padding:32px 32px 8px;">
                            <table cellpadding="0" cellspacing="0" role="presentation">
                                <tr>
                                    <td style="vertical-align:middle;padding-right:10px;">
                                        <img src="{symbol_url}" alt="EulerPro" width="34" height="34" style="display:block;border:0;outline:none;text-decoration:none;" />
                                    </td>
                                    <td style="vertical-align:middle;">
                                        <p style="margin:0;font-size:28px;line-height:1.1;font-weight:800;color:#0f172a;letter-spacing:-0.02em;">EulerPro</p>
                                    </td>
                                </tr>
                            </table>
                            <div style="display:inline-block;margin-top:14px;padding:6px 14px;border-radius:999px;background:#eef2ff;border:1px solid #c7d2fe;">
                                <span style="font-size:12px;font-weight:700;color:#4338ca;letter-spacing:0.02em;">{header_icon} {header_label}</span>
                            </div>
                        </td>
                    </tr>

                    <tr>
                        <td style="padding:10px 32px 34px;">{body_html}</td>
                    </tr>

                    <tr>
                        <td style="padding:0 32px 28px;border-top:1px solid #e2e8f0;">
                            <p style="margin:16px 0 0;font-size:11px;line-height:1.6;color:#94a3b8;text-align:center;">
                                You're receiving this because you have an active EulerPro account.<br/>
                                © 2026 EulerPro - Online Exam and Proctoring Platform.
                            </p>
                        </td>
                    </tr>

                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""


def _cta(text: str, href: str) -> str:
    return (f'<a href="{href}" style="display:inline-block;background:#4f46e5;color:#ffffff;'
            f'font-size:15px;line-height:1;font-weight:800;padding:16px 36px;'
            f'border-radius:999px;text-decoration:none;margin-top:22px;">{text}</a>')


def _detail_row(icon: str, label: str, value: str) -> str:
    return (f'<tr><td style="padding:7px 0;vertical-align:top;"><span style="font-size:14px;">{icon}</span></td>'
            f'<td style="padding:7px 0 7px 10px;vertical-align:top;">'
            f'<span style="font-size:13px;color:#64748b;font-weight:600;">{label}&nbsp;</span>'
            f'<span style="font-size:13px;color:#0f172a;font-weight:700;">{value}</span></td></tr>')


def _info_card(rows_html: str) -> str:
    return (f'<table cellpadding="0" cellspacing="0" style="width:100%;background:#f8fafc;'
            f'border:1px solid #e2e8f0;border-radius:14px;padding:16px 20px;margin:20px 0;">'
            f'<tbody>{rows_html}</tbody></table>')


def _require_instructor(course_id: str, instructor_id: str) -> dict:
    sb = get_supabase()
    r = sb.table("courses").select("*").eq("id", course_id).eq("instructor_id", instructor_id).single().execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Course not found or not yours")
    return r.data


# ── Models ────────────────────────────────────────────────────────────────────

class CourseCreate(BaseModel):
    name: str
    code: Optional[str] = None
    description: Optional[str] = None
    cover_image_url: Optional[str] = None


class CourseUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    cover_image_url: Optional[str] = None


class PublishExamRequest(BaseModel):
    exam_id: str
    instructor_id: str


# ── Course CRUD ───────────────────────────────────────────────────────────────

@router.post("/")
async def create_course(body: CourseCreate, instructor_id: str = Query(...)):
    sb = get_supabase()
    r = sb.table("courses").insert({
        "instructor_id": instructor_id,
        "name": body.name,
        "code": body.code,
        "description": body.description,
        "cover_image_url": body.cover_image_url,
    }).execute()
    if not r.data:
        raise HTTPException(status_code=500, detail="Failed to create course")
    return r.data[0]


@router.get("/")
async def list_courses(instructor_id: str = Query(...)):
    sb = get_supabase()
    r = (sb.table("courses")
         .select("*, course_enrollments(count)")
         .eq("instructor_id", instructor_id)
         .order("created_at", desc=True)
         .execute())
    return r.data or []


@router.get("/{course_id}")
async def get_course(course_id: str, instructor_id: str = Query(...)):
    return _require_instructor(course_id, instructor_id)


@router.patch("/{course_id}")
async def update_course(course_id: str, body: CourseUpdate, instructor_id: str = Query(...)):
    _require_instructor(course_id, instructor_id)
    sb = get_supabase()
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    patch["updated_at"] = _now()
    r = sb.table("courses").update(patch).eq("id", course_id).execute()
    return r.data[0] if r.data else {}


@router.delete("/{course_id}")
async def delete_course(course_id: str, instructor_id: str = Query(...)):
    _require_instructor(course_id, instructor_id)
    sb = get_supabase()
    sb.table("courses").delete().eq("id", course_id).execute()
    return {"ok": True}


# ── CSV Enrollment ─────────────────────────────────────────────────────────────

@router.post("/{course_id}/enroll-csv")
async def enroll_from_csv(
    course_id: str,
    instructor_id: str = Query(...),
    file: UploadFile = File(...),
):
    """
    Upload a CSV with columns: name, email, enrollment_no (optional).
    Students are directly synced — no confirmation step.
    Each student whose email matches an existing Supabase auth user is
    immediately visible in their dashboard. Unknown emails are still stored
    and will be linked when they register.
    After insert, an enrollment notification email is sent via Resend.
    """
    _require_instructor(course_id, instructor_id)

    if not (file.filename or "").endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a .csv")

    raw = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse CSV: {exc}")

    df.columns = [c.strip().lower() for c in df.columns]
    if "email" not in df.columns:
        raise HTTPException(status_code=422, detail="CSV must contain an 'email' column")

    df = df.dropna(subset=["email"]).copy()
    df["email"] = df["email"].str.strip().str.lower()
    df["name"] = df.get("name", df["email"]).fillna(df["email"]).astype(str).str.strip()
    df["enrollment_no"] = df.get("enrollment_no", None)

    rows = [
        {
            "course_id": course_id,
            "student_email": row["email"],
            "student_name": row["name"],
            "enrollment_no": str(row["enrollment_no"]) if pd.notna(row.get("enrollment_no")) else None,
        }
        for _, row in df.iterrows()
        if row["email"]
    ]

    if not rows:
        return {"ok": True, "enrolled": 0, "skipped": 0}

    sb = get_supabase()

    # Upsert — duplicate emails for same course simply update name/enrollment_no
    r = sb.table("course_enrollments").upsert(rows, on_conflict="course_id,student_email").execute()
    enrolled = len(r.data or [])

    # Fetch course + instructor name for notification email
    course_r = sb.table("courses").select("name, code").eq("id", course_id).single().execute()
    course_name = (course_r.data or {}).get("name", "your course")
    course_code = (course_r.data or {}).get("code", "")

    instructor_r = (sb.table("profiles")
                    .select("full_name, email")
                    .eq("id", instructor_id)
                    .single()
                    .execute())
    instructor_name = (instructor_r.data or {}).get("full_name", "Your Instructor")

    # ── In-app notifications ──────────────────────────────────────────────────
    course_label = f"{course_code}: {course_name}" if course_code else course_name
    notif_rows = [
        {
            "student_email": row["student_email"],
            "type": "course_enrolled",
            "title": f"Enrolled in {course_label}",
            "body": f"{instructor_name} added you to {course_label}.",
            "metadata": {
                "course_id": course_id,
                "course_name": course_name,
                "course_code": course_code,
            },
        }
        for row in rows
    ]
    if notif_rows:
        try:
            sb.table("student_notifications").insert(notif_rows).execute()
        except Exception as exc:
            logger.warning("student_notifications insert failed: %s", exc)

    # ── Emails ────────────────────────────────────────────────────────────────
    emails_sent = 0
    if settings.RESEND_API_KEY:
        resend.api_key = settings.RESEND_API_KEY
        for row in rows:
            try:
                subject_course = f"{course_code} — " if course_code else ""
                resend.Emails.send({
                    "from": f"EulerPro <{settings.EMAIL_FROM}>",
                    "to": [row["student_email"]],
                    "subject": f"You've been enrolled in {subject_course}{course_name}",
                    "html": _enrollment_email_html(
                        student_name=row["student_name"],
                        instructor_name=instructor_name,
                        course_name=course_name,
                        course_code=course_code,
                        frontend_url=settings.FRONTEND_URL,
                    ),
                })
                emails_sent += 1
            except Exception as exc:
                logger.warning("Email to %s failed: %s", row["student_email"], exc)

    return {
        "ok": True,
        "enrolled": enrolled,
        "emails_sent": emails_sent,
        "total_rows": len(df),
    }


# ── Roster ────────────────────────────────────────────────────────────────────

@router.get("/{course_id}/roster")
async def get_roster(course_id: str, instructor_id: str = Query(...)):
    _require_instructor(course_id, instructor_id)
    sb = get_supabase()
    r = (sb.table("course_roster")
         .select("*")
         .eq("course_id", course_id)
         .order("enrolled_at", desc=False)
         .execute())
    return r.data or []


class UpdateEnrollmentRequest(BaseModel):
    student_name:  Optional[str] = None
    enrollment_no: Optional[str] = None
    instructor_id: str


@router.patch("/{course_id}/roster/{enrollment_id}")
async def update_student(
    course_id: str,
    enrollment_id: str,
    body: UpdateEnrollmentRequest,
):
    _require_instructor(course_id, body.instructor_id)
    sb = get_supabase()
    patch: dict = {}
    if body.student_name  is not None: patch["student_name"]  = body.student_name.strip() or None
    if body.enrollment_no is not None: patch["enrollment_no"] = body.enrollment_no.strip() or None
    if not patch:
        raise HTTPException(status_code=400, detail="Nothing to update")
    r = sb.table("course_enrollments").update(patch).eq("id", enrollment_id).execute()
    return r.data[0] if r.data else {}


@router.delete("/{course_id}/roster/{enrollment_id}")
async def remove_student(course_id: str, enrollment_id: str, instructor_id: str = Query(...)):
    _require_instructor(course_id, instructor_id)
    sb = get_supabase()
    sb.table("course_enrollments").delete().eq("id", enrollment_id).execute()
    return {"ok": True}


# ── Publish exam blast ────────────────────────────────────────────────────────

@router.post("/{course_id}/publish-exam")
async def publish_exam_blast(course_id: str, body: PublishExamRequest):
    """
    Called when instructor clicks Publish in the exam builder and selects a course.
    1. Marks the exam as 'scheduled' or 'active' (if no future date).
    2. Links exam to the course (sets course_id on exams row).
    3. Sends an exam notification email to every student in the course.
    4. Records the blast in exam_notifications.
    """
    _require_instructor(course_id, body.instructor_id)
    sb = get_supabase()

    # Fetch exam
    exam_r = (sb.table("exams")
              .select("id, title, scheduled_at, status, duration")
              .eq("id", body.exam_id)
              .eq("instructor_id", body.instructor_id)
              .single()
              .execute())
    if not exam_r.data:
        raise HTTPException(status_code=404, detail="Exam not found or not yours")

    exam = exam_r.data
    scheduled_at = exam.get("scheduled_at")
    new_status = (
        "scheduled"
        if scheduled_at and datetime.fromisoformat(scheduled_at) > datetime.now(timezone.utc)
        else "active"
    )

    # Link exam → course and set status
    sb.table("exams").update({
        "course_id": course_id,
        "status": new_status,
    }).eq("id", body.exam_id).execute()

    # Get roster
    roster_r = (sb.table("course_enrollments")
                .select("student_email, student_name")
                .eq("course_id", course_id)
                .execute())
    students = roster_r.data or []

    # ── Populate exam_enrollments for all registered students ─────────────────
    emails = [s["student_email"] for s in students]
    if emails:
        profiles_r = (sb.table("profiles")
                      .select("id, email")
                      .in_("email", emails)
                      .execute())
        email_to_id = {p["email"]: p["id"] for p in (profiles_r.data or [])}
        enrollment_rows = [
            {"exam_id": body.exam_id, "student_id": email_to_id[s["student_email"]]}
            for s in students
            if s["student_email"] in email_to_id
        ]
        if enrollment_rows:
            try:
                sb.table("exam_enrollments").upsert(
                    enrollment_rows,
                    on_conflict="exam_id,student_id",
                    ignore_duplicates=True,
                ).execute()
            except Exception as exc:
                logger.warning("exam_enrollments upsert failed: %s", exc)

    # Instructor info
    instructor_r = (sb.table("profiles")
                    .select("full_name, email")
                    .eq("id", body.instructor_id)
                    .single()
                    .execute())
    instructor_name = (instructor_r.data or {}).get("full_name", "Your Instructor")

    course_r = sb.table("courses").select("name, code").eq("id", course_id).single().execute()
    course_name = (course_r.data or {}).get("name", "")

    exam_url = f"{settings.FRONTEND_URL}/exam/{body.exam_id}/password"

    # ── In-app notifications ──────────────────────────────────────────────────
    notif_rows = [
        {
            "student_email": s["student_email"],
            "type": "exam_published",
            "title": exam["title"],
            "body": f"{instructor_name} published a new exam for {course_name}.",
            "metadata": {
                "exam_id": body.exam_id,
                "course_id": course_id,
                "course_name": course_name,
                "exam_url": exam_url,
                "scheduled_at": scheduled_at,
            },
        }
        for s in students
    ]
    if notif_rows:
        try:
            sb.table("student_notifications").insert(notif_rows).execute()
        except Exception as exc:
            logger.warning("student_notifications insert failed: %s", exc)

    if scheduled_at:
        exam_dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        if exam_dt.tzinfo is None:
            exam_dt = exam_dt.replace(tzinfo=timezone.utc)
        else:
            exam_dt = exam_dt.astimezone(timezone.utc)
        invite_date = exam_dt.strftime('%d %b %Y, %H:%M UTC')
    else:
        invite_date = 'Available now'

    # ── Emails ────────────────────────────────────────────────────────────────
    emails_sent = 0
    if settings.RESEND_API_KEY and students:
        for s in students:
            try:
                notifications.send_exam_invite_email(
                    student_email=s["student_email"],
                    exam_title=exam["title"],
                    exam_date=invite_date,
                    duration_minutes=exam.get("duration", 60),
                    exam_url=exam_url,
                    instructor_name=instructor_name,
                )
                emails_sent += 1
            except Exception as exc:
                logger.warning("Exam email to %s failed: %s", s["student_email"], exc)

    # Schedule one reminder blast 15 minutes before the exam start.
    reminder_scheduled = False
    if scheduled_at and students:
        reminder_scheduled = notifications.schedule_exam_reminder(
            exam_id=body.exam_id,
            exam_title=exam["title"],
            scheduled_at_iso=scheduled_at,
            recipient_emails=[s["student_email"] for s in students if s.get("student_email")],
            exam_url=exam_url,
        )

    # Record blast
    sb.table("exam_notifications").insert({
        "exam_id": body.exam_id,
        "course_id": course_id,
        "sent_by": body.instructor_id,
        "recipient_count": emails_sent,
    }).execute()

    return {
        "ok": True,
        "exam_status": new_status,
        "reminder_scheduled": reminder_scheduled,
        "students_notified": emails_sent,
        "total_enrolled": len(students),
    }


# ── Email templates ───────────────────────────────────────────────────────────

def _enrollment_email_html(student_name: str, instructor_name: str,
                            course_name: str, course_code: str,
                            frontend_url: str) -> str:
    label = f"{course_code}: {course_name}" if course_code else course_name
    body = (
        f'<p style="margin:0 0 6px;font-size:34px;line-height:1.15;font-weight:800;color:#0f172a;letter-spacing:-0.03em;">'
        f'You are enrolled<br/><span style="font-style:italic;font-weight:500;">and all set</span></p>'
        f'<p style="margin:0 0 24px;font-size:15px;line-height:1.55;color:#64748b;">'
        f'Hi {student_name}, <strong style="color:#334155;">{instructor_name}</strong> added you to a new course on EulerPro.</p>'
        + _info_card(
            _detail_row('📚', 'Course', label) +
            _detail_row('👤', 'Instructor', instructor_name)
        ) +
        f'<p style="margin:12px 0 0;font-size:13px;line-height:1.55;color:#64748b;">'
        f'Scheduled exams from this course will appear automatically on your My Exams dashboard.</p>'
        + _cta('Go to My Exams →', f'{frontend_url}/dashboard/student/exams')
    )
    return _shell('Course Enrollment', '📚', body)
