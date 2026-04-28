"""
FastAPI main application
Routes: /api/v1/scoring, /api/v1/notifications, /api/v1/sessions, /api/v1/courses
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .routers import scoring, sessions, notifications, courses


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    print("🚀 YOLOv8 API starting up…")
    notifications.start_scheduler()
    restored = notifications.schedule_existing_exam_reminders()
    print(f"🔔 Reminder scheduler ready ({restored} jobs restored)")
    yield
    notifications.stop_scheduler()
    print("🛑 YOLOv8 API shutting down…")


app = FastAPI(
    title="EulerPro — Exam & Proctoring API",
    description="Backend for scoring, session management, notifications, and courses.",
    version="1.0.2",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(scoring.router, prefix="/api/v1/scoring", tags=["Scoring"])
app.include_router(sessions.router, prefix="/api/v1/sessions", tags=["Sessions"])
app.include_router(notifications.router, prefix="/api/v1/notifications", tags=["Notifications"])
app.include_router(courses.router, prefix="/api/v1/courses", tags=["Courses"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "eulerpro-api"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
