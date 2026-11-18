"""
Configuration settings for Cliptor Clip Studio.
Loads environment variables from .env file with safe defaults.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Find the root project folder
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env if present
load_dotenv(BASE_DIR / ".env")

# Video limits
MAX_VIDEO_MINUTES = int(os.getenv("MAX_VIDEO_MINUTES", "30"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "1000"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Whisper transcription settings:
# Default model "base" for high speed (3-5x faster than small), configurable to tiny/small/medium
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base").strip().lower()

# Hosted transcription mode (optional): "hosted" or "local" (default: "local")
TRANSCRIPTION_MODE = os.getenv("TRANSCRIPTION_MODE", "local").strip().lower()
TRANSCRIPTION_API_BASE = os.getenv(
    "TRANSCRIPTION_API_BASE",
    os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
).strip().rstrip("/")
TRANSCRIPTION_API_KEY = os.getenv(
    "TRANSCRIPTION_API_KEY",
    os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
).strip()
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "whisper-1").strip()

# LLM API configuration (OpenAI-compatible: OpenRouter, OpenAI, Groq, etc.)
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()

# Comma-separated list of fallback models to cycle through if a model is slow or rate limited
_models_env = os.getenv("LLM_MODELS", LLM_MODEL)
LLM_MODELS = [m.strip() for m in _models_env.split(",") if m.strip()]
if LLM_MODEL not in LLM_MODELS:
    LLM_MODELS.insert(0, LLM_MODEL)

# Timeout in seconds per model call before trying the next model
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "60"))

# Clip length constraints (in seconds)
MIN_CLIP_SECONDS = float(os.getenv("MIN_CLIP_SECONDS", "20.0"))
PREFERRED_MIN_SECONDS = float(os.getenv("PREFERRED_MIN_SECONDS", "30.0"))
PREFERRED_MAX_SECONDS = float(os.getenv("PREFERRED_MAX_SECONDS", "90.0"))
MAX_CLIP_SECONDS = float(os.getenv("MAX_CLIP_SECONDS", "120.0"))

# Default vertical layout style: 'blurred_background' (default) or 'center_crop'
DEFAULT_CROP_STYLE = os.getenv("DEFAULT_CROP_STYLE", "blurred_background").strip().lower()

# Directory where each job stores its video, transcript, and clips
JOBS_DIR = BASE_DIR / "jobs_data"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Folder for FFmpeg render logs (logs/clip_01.log)
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Base time limit (seconds) for rendering ONE clip before FFmpeg is killed.
# app/video.py uses max(this, 5 x clip length), so long clips get more time.
RENDER_TIMEOUT_SECONDS = float(os.getenv("RENDER_TIMEOUT_SECONDS", "300"))

# Allowed video extensions
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}

# --- Import from link: streaming sites (YouTube, TikTok, Instagram) ---
# Highest video quality we download. 720p keeps downloads small and fast;
# the format prefers mp4 (avc1 video + m4a audio) and falls back if needed.
YTDLP_MAX_HEIGHT = int(os.getenv("YTDLP_MAX_HEIGHT", "720"))

# Optional: a cookies.txt file exported from a browser. Needed only when a site
# asks to "confirm you're not a bot". Leave empty to stay signed out.
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()

# Optional: read cookies straight from a browser, e.g. "chrome" or "edge".
# Example: YTDLP_COOKIES_FROM_BROWSER=chrome
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

# How long yt-dlp may spend on one video before giving up (seconds)
YTDLP_TIMEOUT_SECONDS = float(os.getenv("YTDLP_TIMEOUT_SECONDS", "1800"))
