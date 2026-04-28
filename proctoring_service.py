"""
Flask proctoring microservice — YOLOv8 + MediaPipe Tasks API + FaceNet
Runs on port 5001, called by the FastAPI backend.

Detections:
  1. Face verification    — FaceNet / MTCNN (verifies registered student)
  2. Head pose            — MediaPipe FaceLandmarker + solvePnP (per-session calibrated)
  3. Multiple persons     — MediaPipe FaceDetector
  4. Electronic devices   — YOLOv8n (cell phone, remote, laptop, book)
  5. Tab switching        — reported by frontend JS, passed in request body
  6. Copy-paste attempts  — reported by frontend JS, passed in request body

Routes:
  GET  /health
  POST /register-face   — store reference FaceNet embedding for a session
  POST /calibrate       — store neutral head-pose baseline for a session
  POST /analyze-frame   — run all detections on a single base64 frame
"""
from __future__ import annotations

import base64
import io
import os
import threading
import urllib.request
from typing import Optional

import cv2
import mediapipe as mp
import numpy as np
import torch
from facenet_pytorch import MTCNN, InceptionResnetV1
from flask import Flask, request, jsonify
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from mediapipe.tasks.python.vision import RunningMode
from PIL import Image, Image as PILImage
from ultralytics import YOLO

app = Flask(__name__)

# ── MediaPipe model paths + auto-download ────────────────────────────────────
_BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
FACE_LANDMARKER_MODEL = os.path.join(_BASE_DIR, "face_landmarker.task")
FACE_DETECTOR_MODEL   = os.path.join(_BASE_DIR, "blaze_face_short_range.tflite")


def _ensure_mp_models() -> None:
    if not os.path.exists(FACE_LANDMARKER_MODEL):
        print("Downloading face_landmarker.task …")
        urllib.request.urlretrieve(
            "https://storage.googleapis.com/mediapipe-models/"
            "face_landmarker/face_landmarker/float16/1/face_landmarker.task",
            FACE_LANDMARKER_MODEL,
        )
    if not os.path.exists(FACE_DETECTOR_MODEL):
        print("Downloading blaze_face_short_range.tflite …")
        urllib.request.urlretrieve(
            "https://storage.googleapis.com/mediapipe-models/"
            "face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
            FACE_DETECTOR_MODEL,
        )


_ensure_mp_models()

# ── MediaPipe models (module-level singletons) ────────────────────────────────
_face_landmarker = mp_vision.FaceLandmarker.create_from_options(
    mp_vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=FACE_LANDMARKER_MODEL),
        running_mode=RunningMode.IMAGE,
        num_faces=4,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
    )
)

_face_detector = mp_vision.FaceDetector.create_from_options(
    mp_vision.FaceDetectorOptions(
        base_options=mp_python.BaseOptions(model_asset_path=FACE_DETECTOR_MODEL),
        running_mode=RunningMode.IMAGE,
    )
)

# ── solvePnP — 6-point 3-D face model (mm scale) ─────────────────────────────
_MODEL_POINTS = np.array([
    (  0.0,    0.0,    0.0),   # nose tip         → landmark 1
    (  0.0, -330.0,  -65.0),   # chin             → landmark 152
    (-225.0,  170.0, -135.0),  # left eye corner  → landmark 33
    ( 225.0,  170.0, -135.0),  # right eye corner → landmark 263
    (-150.0, -150.0, -125.0),  # left mouth       → landmark 61
    ( 150.0, -150.0, -125.0),  # right mouth      → landmark 291
], dtype=np.float64)
_LANDMARK_IDS = [1, 152, 33, 263, 61, 291]

# Head-pose thresholds (degrees, validated against live camera output)
MAX_YAW_OFFSET   = 8.0
MAX_PITCH_OFFSET = 10.0

# Per-session calibrated neutral pose — {session_id: (pitch, yaw)}
_calibration:      dict[str, tuple[float, float]] = {}
_calibration_lock = threading.Lock()

# ── YOLOv8 (lazy-loaded, thread-safe) ────────────────────────────────────────
ELECTRONIC_DEVICE_CLASSES: set[str] = {"cell phone", "remote", "laptop", "book"}

_yolo_model: Optional[YOLO] = None
_yolo_lock = threading.Lock()


def _get_yolo() -> YOLO:
    global _yolo_model
    with _yolo_lock:
        if _yolo_model is None:
            model_path = os.path.join(_BASE_DIR, "yolov8n.pt")
            _yolo_model = YOLO(model_path if os.path.exists(model_path) else "yolov8n.pt")
    return _yolo_model


# ── FaceNet (CPU) ─────────────────────────────────────────────────────────────
_mtcnn   = MTCNN(image_size=160, margin=20, keep_all=False, device="cpu", post_process=True)
_facenet = InceptionResnetV1(pretrained="vggface2").eval()

# Per-session FaceNet embedding cache — {session_id: 512-d tensor}
_face_cache:      dict[str, torch.Tensor] = {}
_face_cache_lock = threading.Lock()

FACE_DIST_THRESH = 0.9   # Euclidean distance threshold in 512-d space


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bytes_to_bgr(image_bytes: bytes) -> Optional[np.ndarray]:
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _frame_to_mp_image(bgr: np.ndarray) -> mp.Image:
    return mp.Image(
        image_format=mp.ImageFormat.SRGB,
        data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
    )


def _normalize_angle(a: float) -> float:
    """
    Fold RQDecomp3x3 output into [-90, 90].
    Without this, forward-facing pitch can read as ~±178° instead of ~0°
    due to gimbal-lock ambiguity in the decomposition.
    """
    a = a % 360
    if a > 180:
        a -= 360
    if a > 90:
        a = 180 - a
    elif a < -90:
        a = -180 - a
    return a


def _estimate_head_pose(landmarks, width: int, height: int) -> Optional[tuple]:
    """
    solvePnP on 6 FaceLandmarker points.
    Returns (pitch, yaw, roll) each normalized to [-90, 90], or None.
    """
    image_points = np.array(
        [(landmarks[i].x * width, landmarks[i].y * height) for i in _LANDMARK_IDS],
        dtype=np.float64,
    )
    cam_matrix = np.array([
        [float(width), 0,            width  / 2],
        [0,            float(width), height / 2],
        [0,            0,            1          ],
    ], dtype=np.float64)

    success, rot_vec, _ = cv2.solvePnP(
        _MODEL_POINTS, image_points, cam_matrix, np.zeros((4, 1))
    )
    if not success:
        return None

    rmat, _    = cv2.Rodrigues(rot_vec)
    angles, *_ = cv2.RQDecomp3x3(rmat)
    return (
        _normalize_angle(angles[0]),
        _normalize_angle(angles[1]),
        _normalize_angle(angles[2]),
    )


def _detect_head_pose(frame_bgr: np.ndarray, session_id: str) -> str:
    """
    Returns "Forward", "LOOKING AWAY", or "No Face".
    Uses per-session calibrated baseline if available (set via /calibrate).
    """
    h, w, _ = frame_bgr.shape
    result   = _face_landmarker.detect(_frame_to_mp_image(frame_bgr))

    if not result.face_landmarks:
        return "No Face"

    angles = _estimate_head_pose(result.face_landmarks[0], w, h)
    if angles is None:
        return "Forward"

    pitch, yaw, _ = angles
    with _calibration_lock:
        cal_pitch, cal_yaw = _calibration.get(session_id, (0.0, 0.0))

    if (abs(pitch - cal_pitch) > MAX_PITCH_OFFSET or
            abs(yaw - cal_yaw) > MAX_YAW_OFFSET):
        return "LOOKING AWAY"
    return "Forward"


def _detect_person_count(frame_bgr: np.ndarray) -> int:
    result = _face_detector.detect(_frame_to_mp_image(frame_bgr))
    return len(result.detections) if result.detections else 0


def _detect_electronic_devices(frame_bgr: np.ndarray) -> list[str]:
    found: list[str] = []
    for result in _get_yolo().predict(
        source=frame_bgr, conf=0.45, save=False, verbose=False
    ):
        for box in result.boxes.cpu().numpy():
            cls_name: str = result.names[int(box.cls[0])]
            if cls_name in ELECTRONIC_DEVICE_CLASSES:
                found.append(cls_name)
    return found


def _get_embedding(frame_bgr: np.ndarray) -> Optional[torch.Tensor]:
    img_pil     = PILImage.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    face_tensor = _mtcnn(img_pil)
    if face_tensor is None:
        return None
    with torch.no_grad():
        return _facenet(face_tensor.unsqueeze(0))[0]


def _verify_face(frame_bgr: np.ndarray, session_id: str) -> str:
    """Returns: verified | unknown | absent | no_reference"""
    with _face_cache_lock:
        known = _face_cache.get(session_id)

    embedding = _get_embedding(frame_bgr)
    if embedding is None:
        return "absent"
    if known is None:
        return "no_reference"

    return "verified" if (embedding - known).norm().item() < FACE_DIST_THRESH else "unknown"


def _build_flags(
    face_status: str,
    person_count: int,
    head_direction: str,
    devices: list[str],
    tab_switches: int = 0,
    copy_paste_attempts: int = 0,
) -> list[dict]:
    flags: list[dict] = []

    if face_status == "absent":
        flags.append({"type": "no_face", "severity": "high",
                      "description": "No person detected in frame"})
    elif face_status == "unknown":
        flags.append({"type": "unknown_face", "severity": "high",
                      "description": "Unrecognised person detected in frame"})

    if person_count > 1:
        flags.append({"type": "multiple_faces", "severity": "high",
                      "description": f"{person_count} people detected in frame"})

    if head_direction == "LOOKING AWAY":
        flags.append({"type": "head_movement", "severity": "medium",
                      "description": "Student looking away from screen"})

    for dev in set(devices):
        flag_type = "phone_detected" if dev == "cell phone" else "electronic_device"
        flags.append({"type": flag_type, "severity": "high",
                      "description": f"Electronic device detected: {dev}"})

    if tab_switches > 0:
        flags.append({"type": "tab_switch", "severity": "medium",
                      "description": f"Tab switched {tab_switches} time(s) since last frame"})

    if copy_paste_attempts > 0:
        flags.append({"type": "copy_paste", "severity": "medium",
                      "description": f"Copy/paste attempted {copy_paste_attempts} time(s) since last frame"})

    return flags


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "blanca-proctoring"})


@app.post("/register-face")
def register_face():
    """
    Cache a FaceNet reference embedding for a session.

    Body (JSON — one of):
      { "session_id": "...", "photo_url": "https://..." }
      { "session_id": "...", "photo_b64": "<base64 JPEG>" }

    photo_url is typically the Supabase Storage URL from photo_verification_url.
    """
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "").strip()
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    image_bytes: Optional[bytes] = None

    if data.get("photo_b64"):
        try:
            image_bytes = base64.b64decode(data["photo_b64"])
        except Exception:
            return jsonify({"error": "Invalid base64 image"}), 422

    elif data.get("photo_url"):
        photo_url = data["photo_url"]
        if not photo_url.startswith("https://"):
            return jsonify({"error": "photo_url must use HTTPS"}), 400
        try:
            req = urllib.request.Request(
                photo_url, headers={"User-Agent": "Blanca-Proctor/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
                image_bytes = resp.read()
        except Exception as exc:
            return jsonify({"error": f"Could not fetch photo: {exc}"}), 422

    if not image_bytes:
        return jsonify({"error": "Provide photo_url or photo_b64"}), 400

    img_bgr = _bytes_to_bgr(image_bytes)
    if img_bgr is None:
        return jsonify({"error": "Invalid image data"}), 422

    embedding = _get_embedding(img_bgr)
    if embedding is None:
        return jsonify({"error": "No face found in reference photo"}), 422

    with _face_cache_lock:
        _face_cache[session_id] = embedding

    return jsonify({"ok": True, "session_id": session_id})


@app.post("/calibrate")
def calibrate():
    """
    Store a neutral head-pose baseline for a session.
    Call this once during the pre-exam system check while the student
    is looking directly at the camera.

    Body (JSON): { "session_id": "...", "frame_b64": "<base64 JPEG>" }

    Returns the measured (pitch, yaw) stored as the session baseline.
    """
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "").strip()
    frame_b64  = data.get("frame_b64", "")

    if not session_id or not frame_b64:
        return jsonify({"error": "Missing session_id or frame_b64"}), 400

    try:
        image_bytes = base64.b64decode(frame_b64)
        Image.open(io.BytesIO(image_bytes)).verify()
    except Exception:
        return jsonify({"error": "Invalid image data"}), 422

    frame_bgr = _bytes_to_bgr(image_bytes)
    if frame_bgr is None:
        return jsonify({"error": "Could not decode frame"}), 422

    h, w, _ = frame_bgr.shape
    result   = _face_landmarker.detect(_frame_to_mp_image(frame_bgr))
    if not result.face_landmarks:
        return jsonify({"error": "No face detected for calibration"}), 422

    angles = _estimate_head_pose(result.face_landmarks[0], w, h)
    if angles is None:
        return jsonify({"error": "Head pose estimation failed"}), 422

    pitch, yaw, _ = angles
    with _calibration_lock:
        _calibration[session_id] = (float(pitch), float(yaw))

    return jsonify({
        "ok": True,
        "session_id": session_id,
        "calibrated_pitch": round(pitch, 3),
        "calibrated_yaw":   round(yaw,   3),
    })


@app.post("/analyze-frame")
def analyze_frame():
    """
    Run all proctoring detections on a single video frame.

    Body (JSON):
      {
        "frame_b64":           "<base64 JPEG>",   # required
        "session_id":          "...",              # required
        "tab_switches":        0,                  # optional — counter from frontend JS
        "copy_paste_attempts": 0                   # optional — counter from frontend JS
      }

    Response:
      {
        "session_id":       "...",
        "ok":               true,
        "face_status":      "verified" | "unknown" | "absent" | "no_reference",
        "person_count":     <int>,
        "head_direction":   "Forward" | "LOOKING AWAY" | "No Face",
        "detected_devices": ["cell phone", ...],
        "flags": [
          { "type": "...", "severity": "low|medium|high", "description": "..." }
        ]
      }
    """
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "unknown")
    frame_b64  = data.get("frame_b64", "")
    tab_switches        = int(data.get("tab_switches", 0))
    copy_paste_attempts = int(data.get("copy_paste_attempts", 0))

    if not frame_b64:
        return jsonify({"error": "Missing frame_b64"}), 400

    try:
        image_bytes = base64.b64decode(frame_b64)
        Image.open(io.BytesIO(image_bytes)).verify()
    except Exception:
        return jsonify({"error": "Invalid image data"}), 422

    frame_bgr = _bytes_to_bgr(image_bytes)
    if frame_bgr is None:
        return jsonify({"error": "Could not decode frame"}), 422

    face_status    = _verify_face(frame_bgr, session_id)
    person_count   = _detect_person_count(frame_bgr)
    head_direction = _detect_head_pose(frame_bgr, session_id)
    devices        = _detect_electronic_devices(frame_bgr)

    flags = _build_flags(
        face_status, person_count, head_direction, devices,
        tab_switches, copy_paste_attempts,
    )

    return jsonify({
        "session_id":       session_id,
        "ok":               True,
        "face_status":      face_status,
        "person_count":     person_count,
        "head_direction":   head_direction,
        "detected_devices": devices,
        "flags":            flags,
    })


if __name__ == "__main__":
    print("Proctoring Service starting on :5001")
    _get_yolo()   # pre-load YOLO at startup to avoid first-request latency
    app.run(host="0.0.0.0", port=5001, debug=True)