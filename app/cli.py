"""
The ``trimly`` command.

    trimly                 start the local web app (127.0.0.1, port 8000)
    trimly --port 8001     use another port (the next free one if it is busy)
    trimly setup           save an optional OpenRouter API key
    trimly doctor          check Python, FFmpeg, the AI key and the Whisper model
    trimly --version       print the version

The server only ever listens on 127.0.0.1, so nothing on the network can reach
your videos.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "trimly"
FALLBACK_VERSION = "0.1.0"
DEFAULT_PORT = 8000
HOST = "127.0.0.1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def app_version() -> str:
    """Read the installed version; fall back when running from the checkout."""
    try:
        from importlib.metadata import version

        return version("trimly")
    except Exception:
        return FALLBACK_VERSION


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _port_is_free(port: int) -> bool:
    """True when nothing is already listening on 127.0.0.1:<port>."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def pick_port(start: int, attempts: int = 20) -> int | None:
    """Returns the first free port at or after ``start``."""
    for port in range(start, start + attempts):
        if _port_is_free(port):
            return port
    return None


def _open_browser_when_ready(url: str) -> None:
    """Waits until the local server answers, then opens the browser once."""
    import urllib.request

    for _ in range(120):  # up to about one minute
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.5)


def _set_line(lines: list[str], key: str, value: str) -> list[str]:
    """Replace ``KEY=...`` in place, or append it when it is missing."""
    prefix = key + "="
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.strip().startswith(prefix):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    return output


def _read_line(lines: list[str], key: str, default: str = "") -> str:
    prefix = key + "="
    for line in lines:
        if line.strip().startswith(prefix):
            return line.split("=", 1)[1].strip() or default
    return default


def _ask(prompt: str) -> str:
    """input() that returns '' instead of crashing when input is piped."""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


# ---------------------------------------------------------------------------
# trimly            (start the server)
# ---------------------------------------------------------------------------

def run_server(requested_port: int) -> int:
    from app.config import LLM_API_KEY, WHISPER_MODEL
    from app.models import download_model, is_model_downloaded, model_size_mb
    from app.paths import CONFIG_FILE, DATA_DIR

    if not is_model_downloaded(WHISPER_MODEL):
        size = model_size_mb(WHISPER_MODEL)
        print()
        print(f"First run: the Whisper '{WHISPER_MODEL}' speech model must be downloaded.")
        print(f"Approximate size: {size} MB. A progress bar is shown below;")
        print("this usually takes 1-5 minutes and happens only once.")
        print()
        try:
            download_model(WHISPER_MODEL)
            print("Speech model ready.")
        except Exception as error:
            print(f"Could not download the model now ({error}).")
            print("Trimly will try again when you process your first video.")

    if not LLM_API_KEY:
        print(
            "No AI key found: using Basic suggestions mode. "
            "Run `trimly setup` for smarter highlight picks."
        )

    port = pick_port(requested_port)
    if port is None:
        print(
            f"No free port between {requested_port} and {requested_port + 19}. "
            "Close the other app or pass --port with a different number."
        )
        return 1

    if port != requested_port:
        print(f"Port {requested_port} is busy, using {port} instead.")

    url = f"http://{HOST}:{port}"
    print(f"Starting Trimly Clip Studio at {url}")
    print(f"Settings: {CONFIG_FILE}")
    print(f"Jobs and logs: {DATA_DIR}")
    print("Keep this window open. Press Ctrl+C to stop.")

    # TRIMLY_NO_BROWSER=1 is handy for tests and headless setups.
    no_browser = os.environ.get("TRIMLY_NO_BROWSER", "").strip().lower() in {"1", "true", "yes"}
    if not no_browser:
        threading.Thread(
            target=_open_browser_when_ready, args=(url,), daemon=True
        ).start()

    import uvicorn

    # Binding to 127.0.0.1 keeps the app private to this computer.
    uvicorn.run("app.main:app", host=HOST, port=port, log_level="info")
    return 0


# ---------------------------------------------------------------------------
# trimly setup
# ---------------------------------------------------------------------------

def run_setup() -> int:
    from app.paths import CONFIG_FILE

    print(f"Config file: {CONFIG_FILE}")
    lines: list[str] = []
    if CONFIG_FILE.exists():
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()

    current_key = _read_line(lines, "LLM_API_KEY")
    if current_key:
        print("An API key is already saved. Press Enter to keep it.")

    try:
        # getpass hides the key but needs a real console. When input is piped
        # (tests, scripts) fall back to a normal prompt.
        if sys.stdin.isatty():
            typed_key = getpass.getpass("OpenRouter API key (optional, Enter to skip): ").strip()
        else:
            typed_key = _ask("OpenRouter API key (optional, Enter to skip): ")
    except Exception:
        typed_key = _ask("OpenRouter API key (optional, Enter to skip): ")

    key = typed_key or current_key
    current_models = _read_line(lines, "LLM_MODELS") or _read_line(
        lines, "LLM_MODEL", DEFAULT_MODEL
    )
    typed_models = _ask(f"Model(s), comma separated [Enter keeps: {current_models}]: ")
    model_list = typed_models or current_models

    models = [m.strip() for m in model_list.split(",") if m.strip()]
    if not models:
        models = [DEFAULT_MODEL]

    lines = _set_line(lines, "LLM_API_KEY", key)
    lines = _set_line(lines, "LLM_BASE_URL", OPENROUTER_URL)
    lines = _set_line(lines, "LLM_MODEL", models[0])
    lines = _set_line(lines, "LLM_MODELS", ", ".join(models))

    if not any(line.strip().startswith("# Trimly settings") for line in lines):
        lines.insert(0, "# Trimly settings (created by `trimly setup`)")
        lines.insert(1, "")

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Best effort: keep the key private on POSIX systems.
    if os.name != "nt":
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass

    if key:
        print(f"Saved your API key and {len(models)} model(s) to {CONFIG_FILE}.")
        print("Start Trimly with: trimly")
    else:
        print("No key saved. Trimly will run in Basic suggestions mode.")
    return 0


# ---------------------------------------------------------------------------
# trimly doctor
# ---------------------------------------------------------------------------

def _mask(key: str) -> str:
    if not key:
        return "(not set)"
    return key[:6] + "..." + key[-4:] if len(key) > 12 else "(set)"


def run_doctor() -> int:
    from app.config import LLM_API_KEY, LLM_MODEL, WHISPER_MODEL
    from app.models import cache_folder, is_model_downloaded, model_size_mb
    from app.paths import CONFIG_FILE, DATA_DIR, IS_INSTALLED

    problems = 0

    def report(ok: bool, label: str, detail: str, fix: str = "") -> None:
        nonlocal problems
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {label}: {detail}")
        if not ok:
            problems += 1
            if fix:
                print(f"       Fix: {fix}")

    print("Trimly doctor")
    print("=" * 60)

    # 1. Python version. The package supports 3.11, 3.12 and 3.13.
    py_ok = (3, 11) <= sys.version_info[:2] < (3, 14)
    report(
        py_ok,
        "Python",
        f"{sys.version.split()[0]} ({sys.executable})",
        "Install Python 3.12 from https://www.python.org/downloads/ and reinstall "
        "Trimly with: uv tool install --python 3.12 --force "
        "git+https://github.com/USERNAME/trimly.git",
    )

    # 2. FFmpeg and ffprobe. winget installs them inside a package folder that
    # is often not on PATH yet, so look next to ffmpeg as well.
    from app.video import find_ffmpeg_executable

    ffmpeg = find_ffmpeg_executable()
    ffprobe = shutil.which("ffprobe")
    if not ffprobe and ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe.exe")
        ffprobe = str(candidate) if candidate.is_file() else None
    report(
        bool(ffmpeg),
        "FFmpeg",
        ffmpeg or "not found",
        "Run: winget install Gyan.FFmpeg  (then open a new terminal)",
    )
    report(
        bool(ffprobe),
        "ffprobe",
        ffprobe or "not found",
        "After installing FFmpeg, close and reopen this terminal.",
    )

    # 3. The AI key is optional, so it is never a failure.
    if LLM_API_KEY:
        print(f"[OK  ] AI key: {_mask(LLM_API_KEY)}  (model: {LLM_MODEL})")
    else:
        print("[INFO] AI key: not set - Basic suggestions mode (offline)")

    # 4. Whisper model cache.
    model_ok = is_model_downloaded(WHISPER_MODEL)
    report(
        model_ok,
        "Whisper model",
        (
            f"'{WHISPER_MODEL}' ready"
            if model_ok
            else f"'{WHISPER_MODEL}' not downloaded yet "
            f"(~{model_size_mb(WHISPER_MODEL)} MB, downloads on first run)"
        ),
        "Start Trimly once with an internet connection: trimly",
    )

    # 5. Folders and config.
    writable = False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as error:
        print(f"       ({error})")
    report(
        writable,
        "Data folder",
        str(DATA_DIR),
        "Check disk permissions or set TRIMLY_DATA_DIR to a writable folder.",
    )
    print(f"[INFO] Config file: {CONFIG_FILE} ({'found' if CONFIG_FILE.exists() else 'not created yet'})")
    print(f"[INFO] Mode: {'installed package' if IS_INSTALLED else 'source checkout'}")
    print(f"[INFO] Model cache: {cache_folder(WHISPER_MODEL)}")

    print("=" * 60)
    if problems:
        print(f"{problems} problem(s) found. Apply the fixes above and run `trimly doctor` again.")
        return 1
    print("Everything looks good. Start Trimly with: trimly")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Trimly Clip Studio - turn long videos into vertical clips locally.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {app_version()}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port for the local web app (default: {DEFAULT_PORT})",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("setup", help="save an optional OpenRouter API key")
    subparsers.add_parser("doctor", help="check Python, FFmpeg, AI key and Whisper model")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "setup":
        return run_setup()
    if args.command == "doctor":
        return run_doctor()
    return run_server(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
