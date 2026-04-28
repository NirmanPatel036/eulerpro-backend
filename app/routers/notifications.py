"""
Notifications router — email via Resend.dev
POST /api/v1/notifications/exam-invite
POST /api/v1/notifications/results
POST /api/v1/notifications/reminder
"""
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, EmailStr
import resend
from ..database import get_supabase
from ..config import settings

router = APIRouter()
resend.api_key = settings.RESEND_API_KEY
logger = logging.getLogger(__name__)

REMINDER_LEAD_MINUTES = 15
EXAM_PASSWORD_LENGTH = 6
EXAM_PASSWORD_WINDOW_MINUTES = 15
_EXAM_PASSWORD_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_scheduled_reminders: dict[str, asyncio.Task] = {}

# ── Shared email shell ────────────────────────────────────────────────────────


def _brand_symbol_url() -> str:
    """Absolute URL for brand symbol image in emails."""
    return f"{settings.FRONTEND_URL.rstrip('/')}/symbol.png"


def _invite_shell(body_html: str) -> str:
    """Invite-specific shell aligned to the scheduled exam theme."""
    symbol_url = _brand_symbol_url()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Exam Scheduled - EulerPro</title>
</head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:Inter,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
         style="background:#f9fafb;padding:24px 12px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" role="presentation"
               style="max-width:600px;width:100%;background:#ffffff;border-radius:20px;
                      border:1px solid #e2e8f0;overflow:hidden;box-shadow:0 8px 30px rgba(15,23,42,0.05);">

          <tr>
            <td align="center" style="padding:36px 32px 12px;">
              <table cellpadding="0" cellspacing="0" role="presentation">
                <tr>
                  <td style="vertical-align:middle;padding-right:10px;">
                    <img src="{symbol_url}" alt="EulerPro" width="34" height="34"
                         style="display:block;border:0;outline:none;text-decoration:none;" />
                  </td>
                  <td style="vertical-align:middle;">
                    <p style="margin:0;font-size:28px;line-height:1.1;font-weight:800;color:#0f172a;letter-spacing:-0.02em;">
                      EulerPro
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:10px 32px 34px;">
              {body_html}
            </td>
          </tr>

          <tr>
            <td style="padding:0 32px 30px;border-top:1px solid #e2e8f0;">
              <p style="margin:18px 0 0;font-size:11px;line-height:1.6;color:#94a3b8;text-align:center;">
                You're receiving this because you have an active EulerPro account.
                <br/>© 2026 EulerPro - Online Exam and Proctoring Platform.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _modern_shell(title: str, body_html: str) -> str:
    """Reusable light shell used by latest email templates."""
    symbol_url = _brand_symbol_url()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:Inter,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
         style="background:#f9fafb;padding:24px 12px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" role="presentation"
               style="max-width:600px;width:100%;background:#ffffff;border-radius:20px;
                      border:1px solid #e2e8f0;overflow:hidden;box-shadow:0 8px 30px rgba(15,23,42,0.05);">
          <tr>
            <td align="center" style="padding:32px 32px 8px;">
              <table cellpadding="0" cellspacing="0" role="presentation">
                <tr>
                  <td style="vertical-align:middle;padding-right:10px;">
                    <img src="{symbol_url}" alt="EulerPro" width="34" height="34"
                         style="display:block;border:0;outline:none;text-decoration:none;" />
                  </td>
                  <td style="vertical-align:middle;">
                    <p style="margin:0;font-size:28px;line-height:1.1;font-weight:800;color:#0f172a;letter-spacing:-0.02em;">EulerPro</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 32px 34px;">{body_html}</td>
          </tr>
          <tr>
            <td style="padding:0 32px 28px;border-top:1px solid #e2e8f0;">
              <p style="margin:16px 0 0;font-size:11px;line-height:1.6;color:#94a3b8;text-align:center;">
                You're receiving this because you have an active EulerPro account.
                <br/>© 2026 EulerPro - Online Exam and Proctoring Platform.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ── Templates ──────────────────────────────────────────────────────────────────

def _invite_html(
    exam_title: str,
    date: str,
    duration: int,
    link: str,
    instructor_name: str | None = None,
) -> str:
    instructor_row = ""
    if instructor_name:
        instructor_row = f"""
                  <tr>
                    <td style="width:50%;vertical-align:top;padding-right:8px;padding-top:14px;">
                      <p style="margin:0 0 5px;font-size:11px;line-height:1.2;color:#94a3b8;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">Instructor</p>
                      <p style="margin:0;font-size:14px;line-height:1.4;color:#0f172a;font-weight:700;">{instructor_name}</p>
                    </td>
                    <td style="width:50%;vertical-align:top;padding-left:8px;padding-top:14px;">&nbsp;</td>
                  </tr>
        """

    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td align="center" style="padding:6px 0 8px;">
          <p style="margin:0;font-size:40px;line-height:1.15;font-weight:800;color:#0f172a;letter-spacing:-0.03em;">
            Exam scheduled for
            <br/>
            <span style="font-style:italic;font-weight:500;">your success</span>
          </p>
        </td>
      </tr>
      <tr>
        <td align="center" style="padding:0 0 24px;">
          <p style="margin:0;max-width:430px;font-size:16px;line-height:1.55;color:#64748b;">
            Get ready for your upcoming assessment. We have reserved your spot on the EulerPro platform.
          </p>
        </td>
      </tr>
      <tr>
        <td>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                 style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:22px 20px;margin:0 0 24px;">
            <tr>
              <td style="padding-bottom:12px;border-bottom:1px solid #e2e8f0;">
                <p style="margin:0 0 5px;font-size:22px;line-height:1.25;font-weight:800;color:#0f172a;">{exam_title}</p>
                <p style="margin:0;font-size:14px;line-height:1.5;color:#475569;font-weight:600;">{date}</p>
              </td>
            </tr>
            <tr>
              <td style="padding-top:14px;">
                <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
                  <tr>
                    <td style="width:50%;vertical-align:top;padding-right:8px;">
                      <p style="margin:0 0 5px;font-size:11px;line-height:1.2;color:#94a3b8;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">Duration</p>
                      <p style="margin:0;font-size:14px;line-height:1.4;color:#0f172a;font-weight:700;">{duration} Minutes</p>
                    </td>
                    <td style="width:50%;vertical-align:top;padding-left:8px;">
                      <p style="margin:0 0 5px;font-size:11px;line-height:1.2;color:#94a3b8;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">Status</p>
                      <p style="margin:0;font-size:14px;line-height:1.4;color:#047857;font-weight:700;">Confirmed</p>
                    </td>
                  </tr>
                  {instructor_row}
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td align="center" style="padding-top:2px;">
          <a href="{link}"
             style="display:inline-block;background:#4f46e5;color:#ffffff;text-decoration:none;
                    font-size:15px;line-height:1;font-weight:800;padding:16px 36px;border-radius:999px;">
            Take Exam
          </a>
        </td>
      </tr>
    </table>
    """
    return _invite_shell(body)


def _results_html(student_name: str, exam_title: str, percentage: float, passed: bool, link: str) -> str:
    verdict_color = "#047857" if passed else "#b91c1c"
    verdict_bg = "#ecfdf5" if passed else "#fef2f2"
    verdict_border = "#a7f3d0" if passed else "#fecaca"
    verdict_label = "Passed" if passed else "Not Passed"

    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td align="center" style="padding:6px 0 8px;">
          <p style="margin:0;font-size:36px;line-height:1.15;font-weight:800;color:#0f172a;letter-spacing:-0.03em;">
            Results ready for
            <br/>
            <span style="font-style:italic;font-weight:500;">your review</span>
          </p>
        </td>
      </tr>
      <tr>
        <td align="center" style="padding:0 0 22px;">
          <p style="margin:0;max-width:440px;font-size:16px;line-height:1.55;color:#64748b;">
            Hi {student_name}, your performance summary for {exam_title} is now available.
          </p>
        </td>
      </tr>
      <tr>
        <td>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                 style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:24px 20px;margin:0 0 24px;">
            <tr>
              <td align="center" style="padding-bottom:14px;border-bottom:1px solid #e2e8f0;">
                <p style="margin:0;font-size:56px;line-height:1;font-weight:900;color:#0f172a;letter-spacing:-0.03em;">{percentage:.0f}<span style="font-size:30px;">%</span></p>
              </td>
            </tr>
            <tr>
              <td align="center" style="padding-top:14px;">
                <span style="display:inline-block;background:{verdict_bg};border:1px solid {verdict_border};color:{verdict_color};font-size:13px;line-height:1;font-weight:800;padding:8px 14px;border-radius:999px;">{verdict_label}</span>
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td align="center">
          <a href="{link}"
             style="display:inline-block;background:#4f46e5;color:#ffffff;text-decoration:none;font-size:15px;line-height:1;font-weight:800;padding:16px 36px;border-radius:999px;">
            View Results
          </a>
        </td>
      </tr>
    </table>
    """
    return _modern_shell("Results Ready - EulerPro", body)


def _reminder_html(exam_title: str, minutes_until_start: int, link: str, exam_password: str | None = None) -> str:
    if minutes_until_start >= 60 and minutes_until_start % 60 == 0:
        hours = minutes_until_start // 60
        starts_label = f"{hours} hour{'s' if hours != 1 else ''}"
    else:
        starts_label = f"{minutes_until_start} minute{'s' if minutes_until_start != 1 else ''}"

    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td align="center" style="padding:6px 0 8px;">
          <p style="margin:0;font-size:36px;line-height:1.15;font-weight:800;color:#0f172a;letter-spacing:-0.03em;">
            Your exam starts
            <br/>
            <span style="font-style:italic;font-weight:500;">very soon</span>
          </p>
        </td>
      </tr>
      <tr>
        <td align="center" style="padding:0 0 22px;">
          <p style="margin:0;max-width:430px;font-size:16px;line-height:1.55;color:#64748b;">
            Complete your system setup now so you can begin without delays.
          </p>
        </td>
      </tr>
      <tr>
        <td>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                 style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:22px 20px;margin:0 0 18px;">
            <tr>
              <td style="padding-bottom:12px;border-bottom:1px solid #e2e8f0;">
                <p style="margin:0 0 5px;font-size:22px;line-height:1.25;font-weight:800;color:#0f172a;">{exam_title}</p>
                <p style="margin:0;font-size:14px;line-height:1.5;color:#475569;font-weight:600;">Starts in {starts_label}</p>
              </td>
            </tr>
            <tr>
              <td style="padding-top:14px;">
                <p style="margin:0;font-size:13px;line-height:1.55;color:#64748b;">Ensure a stable internet connection, active webcam, and quiet environment before launch.</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        {f'''<td>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                 style="background:#eef2ff;border:1px solid #c7d2fe;border-radius:16px;padding:16px 20px;margin:0 0 18px;">
            <tr>
              <td align="center">
                <p style="margin:0 0 6px;font-size:11px;line-height:1.2;color:#6366f1;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;">Exam Password</p>
                <p style="margin:0;font-size:30px;line-height:1;font-weight:900;color:#312e81;letter-spacing:0.14em;">{exam_password}</p>
                <p style="margin:8px 0 0;font-size:12px;line-height:1.5;color:#4f46e5;font-weight:600;">Valid from 15 minutes before start until 15 minutes after scheduled time.</p>
              </td>
            </tr>
          </table>
        </td>''' if exam_password else ''}
      </tr>
      <tr>
        <td align="center">
          <a href="{link}"
             style="display:inline-block;background:#4f46e5;color:#ffffff;text-decoration:none;font-size:15px;line-height:1;font-weight:800;padding:16px 36px;border-radius:999px;">
            Take Exam
          </a>
        </td>
      </tr>
    </table>
    """
    return _modern_shell("Exam Reminder - EulerPro", body)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _send(to: list[str], subject: str, html: str):
  """Fire-and-forget email send.

  Send one recipient per request so sandbox/test-mode failures do not
  block allowed recipients in the same batch.
  """
  recipients = sorted({email.strip().lower() for email in to if email and email.strip()})
  if not recipients:
    logger.info("Email skipped: empty recipient list for subject '%s'", subject)
    return

  sent_count = 0
  failed_recipients: list[str] = []

  for recipient in recipients:
    try:
      resend.Emails.send({
        "from": f"EulerPro <{settings.EMAIL_FROM}>",
        "to": [recipient],
        "subject": subject,
        "html": html,
      })
      sent_count += 1
    except Exception as exc:
      failed_recipients.append(recipient)
      logger.warning("Email failed to %s: %s", recipient, exc)

  logger.info(
    "Email batch complete | subject='%s' | attempted=%d | sent=%d | failed=%d",
    subject,
    len(recipients),
    sent_count,
    len(failed_recipients),
  )
  if failed_recipients:
    logger.warning("Email batch failed recipients for subject '%s': %s", subject, failed_recipients)


def generate_exam_password(*, exam_id: str, scheduled_at_iso: str) -> str:
    """Create a stable 6-char uppercase alphanumeric password for an exam window."""
    starts_at = _parse_iso_utc(scheduled_at_iso)
    digest = hashlib.sha256(f"{exam_id}|{starts_at.isoformat()}".encode("utf-8")).digest()
    number = int.from_bytes(digest[:8], "big")
    chars: list[str] = []
    base = len(_EXAM_PASSWORD_ALPHABET)
    for _ in range(EXAM_PASSWORD_LENGTH):
        number, idx = divmod(number, base)
        chars.append(_EXAM_PASSWORD_ALPHABET[idx])
    return "".join(chars)


def send_exam_invite_email(
    *,
    student_email: str,
    exam_title: str,
    exam_date: str,
    duration_minutes: int,
    exam_url: str,
    instructor_name: str | None = None,
) -> bool:
    """Send the canonical exam invite email to one recipient."""
    _send(
        [student_email],
        f"Exam Invite: {exam_title}",
    _invite_html(exam_title, exam_date, duration_minutes, exam_url, instructor_name),
    )
    return True


# ── Models ────────────────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    student_emails: list[EmailStr]
    exam_title: str
    exam_date: str
    duration_minutes: int
    exam_url: str
    instructor_name: str | None = None


class ResultsRequest(BaseModel):
    student_email: EmailStr
    student_name: str
    exam_title: str
    percentage: float
    passed: bool
    results_url: str


class ReminderRequest(BaseModel):
    student_emails: list[EmailStr]
    exam_title: str
    starts_in_hours: int
    exam_url: str
    exam_password: str | None = None


class VerifyExamPasswordRequest(BaseModel):
    exam_id: str
    password: str


def _parse_iso_utc(iso_str: str) -> datetime:
  dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
  if dt.tzinfo is None:
    return dt.replace(tzinfo=timezone.utc)
  return dt.astimezone(timezone.utc)


def start_scheduler() -> None:
  # No-op for asyncio-based scheduler; tasks are created on demand.
  return None


def stop_scheduler() -> None:
  for task in _scheduled_reminders.values():
    task.cancel()
  _scheduled_reminders.clear()


def _clear_scheduled_task(task_id: str) -> None:
  current = _scheduled_reminders.get(task_id)
  if current and current.done():
    _scheduled_reminders.pop(task_id, None)


async def _send_reminder_at(
  *,
  task_id: str,
  delay_seconds: float,
  recipients: list[str],
  exam_title: str,
  exam_url: str,
  exam_password: str,
) -> None:
  try:
    if delay_seconds > 0:
      await asyncio.sleep(delay_seconds)
    _send(
      recipients,
      f"Reminder: {exam_title} starts in {REMINDER_LEAD_MINUTES}m | Password: {exam_password}",
      _reminder_html(exam_title, REMINDER_LEAD_MINUTES, exam_url, exam_password),
    )
  except asyncio.CancelledError:
    raise
  except Exception:
    logger.exception("Failed to send reminder for %s", task_id)
  finally:
    _scheduled_reminders.pop(task_id, None)


def schedule_exam_reminder(
  *,
  exam_id: str,
  exam_title: str,
  scheduled_at_iso: str,
  recipient_emails: list[str],
  exam_url: str,
) -> bool:
  """Schedule one reminder send exactly 15 minutes before exam start.

  If the API starts or the exam is published inside the 15-minute window,
  send the reminder immediately instead of skipping it.
  """
  if not recipient_emails:
    logger.info("Skipping reminder for exam %s: no recipients", exam_id)
    return False

  try:
    starts_at_utc = _parse_iso_utc(scheduled_at_iso)
  except ValueError:
    logger.warning("Invalid scheduled_at for exam %s: %s", exam_id, scheduled_at_iso)
    return False

  run_at = starts_at_utc - timedelta(minutes=REMINDER_LEAD_MINUTES)
  now = datetime.now(timezone.utc)
  if starts_at_utc <= now:
    logger.info("Skipping reminder for exam %s: exam already started", exam_id)
    return False

  exam_password = generate_exam_password(exam_id=exam_id, scheduled_at_iso=scheduled_at_iso)

  task_id = f"exam-reminder-{exam_id}"
  existing = _scheduled_reminders.get(task_id)
  if existing and not existing.done():
    existing.cancel()
  elif existing and existing.done():
    _clear_scheduled_task(task_id)

  delay_seconds = max((run_at - now).total_seconds(), 0)
  if delay_seconds == 0:
    logger.info("Reminder for exam %s is within the 15-minute window; sending immediately", exam_id)
  else:
    logger.info("Scheduled reminder for exam %s in %.0f seconds", exam_id, delay_seconds)

  task = asyncio.create_task(
    _send_reminder_at(
      task_id=task_id,
      delay_seconds=delay_seconds,
      recipients=recipient_emails,
      exam_title=exam_title,
      exam_url=exam_url,
      exam_password=exam_password,
    )
  )
  _scheduled_reminders[task_id] = task
  return True


def schedule_existing_exam_reminders() -> int:
  """Rehydrate reminder jobs on startup for future scheduled exams."""
  sb = get_supabase()
  now_iso = datetime.now(timezone.utc).isoformat()
  exams = (
    sb.table("exams")
    .select("id, title, scheduled_at, course_id")
    .not_.is_("scheduled_at", "null")
    .gt("scheduled_at", now_iso)
    .execute()
  )

  scheduled = 0
  for exam in (exams.data or []):
    course_id = exam.get("course_id")
    if not course_id:
      continue

    roster = (
      sb.table("course_enrollments")
      .select("student_email")
      .eq("course_id", course_id)
      .execute()
    )
    emails = [r["student_email"] for r in (roster.data or []) if r.get("student_email")]
    exam_url = f"{settings.FRONTEND_URL}/exam/{exam['id']}/password"

    if schedule_exam_reminder(
      exam_id=exam["id"],
      exam_title=exam.get("title") or "Exam",
      scheduled_at_iso=exam["scheduled_at"],
      recipient_emails=emails,
      exam_url=exam_url,
    ):
      scheduled += 1

  return scheduled


def send_result_email_for_submission(
  *,
  session_id: str,
  exam_id: str,
  percentage: float,
  passed: bool,
) -> bool:
  """Send result email immediately after final submit."""
  sb = get_supabase()
  session = (
    sb.table("exam_sessions")
    .select("student_id")
    .eq("id", session_id)
    .single()
    .execute()
  )
  if not session.data or not session.data.get("student_id"):
    logger.warning("Result email skipped: session/student missing for %s", session_id)
    return False

  student_id = session.data["student_id"]
  profile = (
    sb.table("profiles")
    .select("email, full_name")
    .eq("id", student_id)
    .single()
    .execute()
  )
  if not profile.data or not profile.data.get("email"):
    logger.warning("Result email skipped: student email missing for session %s", session_id)
    return False

  exam = (
    sb.table("exams")
    .select("title")
    .eq("id", exam_id)
    .single()
    .execute()
  )
  exam_title = (exam.data or {}).get("title") or "Exam"
  student_name = profile.data.get("full_name") or "Student"
  results_url = f"{settings.FRONTEND_URL}/exam/{exam_id}/results?session={session_id}"

  _send(
    [profile.data["email"]],
    f"Your results for {exam_title}",
    _results_html(student_name, exam_title, percentage, passed, results_url),
  )
  return True


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/exam-invite")
async def send_exam_invite(req: InviteRequest, bg: BackgroundTasks):
    html = _invite_html(
        req.exam_title,
        req.exam_date,
        req.duration_minutes,
        req.exam_url,
        req.instructor_name,
    )
    bg.add_task(_send, req.student_emails, f"Exam Invite: {req.exam_title}", html)
    return {"ok": True, "recipients": len(req.student_emails)}


@router.post("/results")
async def send_results_email(req: ResultsRequest, bg: BackgroundTasks):
    html = _results_html(req.student_name, req.exam_title, req.percentage, req.passed, req.results_url)
    subject = f"Your results for {req.exam_title}"
    bg.add_task(_send, [req.student_email], subject, html)
    return {"ok": True}


@router.post("/reminder")
async def send_reminder(req: ReminderRequest, bg: BackgroundTasks):
  minutes = req.starts_in_hours * 60
  html = _reminder_html(req.exam_title, minutes, req.exam_url, req.exam_password)
  subject = f"Reminder: {req.exam_title} starts in {req.starts_in_hours}h"
  if req.exam_password:
    subject += f" | Password: {req.exam_password}"

  bg.add_task(
    _send,
    req.student_emails,
    subject,
    html,
  )
  return {"ok": True, "recipients": len(req.student_emails)}


@router.post("/exam-password/verify")
async def verify_exam_password(req: VerifyExamPasswordRequest):
    sb = get_supabase()
    exam_r = (
        sb.table("exams")
        .select("id, scheduled_at")
        .eq("id", req.exam_id)
        .single()
        .execute()
    )
    if not exam_r.data:
        return {"ok": False, "error": "Exam not found"}

    scheduled_at = exam_r.data.get("scheduled_at")
    if not scheduled_at:
        return {"ok": False, "error": "This exam has no scheduled time"}

    try:
        starts_at = _parse_iso_utc(scheduled_at)
    except ValueError:
        return {"ok": False, "error": "Invalid exam schedule"}

    now = datetime.now(timezone.utc)
    window_start = starts_at - timedelta(minutes=EXAM_PASSWORD_WINDOW_MINUTES)
    window_end = starts_at + timedelta(minutes=EXAM_PASSWORD_WINDOW_MINUTES)

    if now < window_start:
        return {
            "ok": False,
            "error": "Password entry opens 15 minutes before the scheduled start time",
            "window_starts_at": window_start.isoformat(),
        }
    if now > window_end:
        return {
            "ok": False,
            "error": "Password expired. Late entry is not allowed after 15 minutes",
            "expired_at": window_end.isoformat(),
        }

    expected = generate_exam_password(exam_id=req.exam_id, scheduled_at_iso=scheduled_at)
    if req.password.strip().upper() != expected:
        return {"ok": False, "error": "Incorrect exam password"}

    return {
        "ok": True,
        "expires_at": window_end.isoformat(),
    }
