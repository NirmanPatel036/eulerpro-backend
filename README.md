# Backend Setup

FastAPI backend for scoring, sessions, notifications, courses, and proctoring integration.

## Requirements

- Python 3.11+
- pip

## Install

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate with:

```bash
.venv\Scripts\activate
```

## Environment

Create `backend/.env`:

```env
SUPABASE_URL=your_supabase_url
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key

RESEND_API_KEY=your_resend_api_key
EMAIL_FROM=no-reply@example.com

FRONTEND_URL=http://localhost:3000
PROCTORING_SERVICE_URL=http://localhost:5001
ALLOWED_ORIGINS=["http://localhost:3000"]
```

## Run FastAPI Backend

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
```

## Run Proctoring Service

In another terminal:

```bash
cd backend
source .venv/bin/activate
python proctoring_service.py
```

The proctoring service runs on `http://localhost:5001`.
