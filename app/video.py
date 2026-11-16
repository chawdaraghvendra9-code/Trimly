"""
FFmpeg utilities for Cliptor Clip Studio.
Safely calls FFmpeg using argument lists (never raw shell strings).
Handles audio extraction, center cropping, blurred background vertical rendering,
and burning in ASS subtitles.
"""
import os
import shlex
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Optional

from app.config import LOGS_DIR, RENDER_TIMEOUT_SECONDS

logger = logging.getLogger("cliptor")


class RenderError(RuntimeError):
    """Raised when FFmpeg fails, times out, or produces an empty clip."""


def render_timeout_for(clip_duration: float) -> float:
    """Time limit for one clip: at least RENDER_TIMEOUT_SECONDS, and at least 5x the clip length."""
    return max(RENDER_TIMEOUT_SECONDS, 5.0 * clip_duration)


def get_render_log_path(output_mp4_path: Path) -> Path:
    """Returns logs/clip_01.log for an output file clip_01.mp4."""
    return LOGS_DIR / f"{output_mp4_path.stem}.log"


def _escape_filter_text(text: str) -> str:
    """Escapes simple text for an FFmpeg filter: forward slashes, drive colon, quotes."""
    text = text.replace("\\", "/")
    text = text.replace(":", "\\:")   # escape the drive colon (C:)
    text = text.replace("'", "\\'")   # escape apostrophes (e.g. O'Brien)
    return f"'{text}'"                # single quotes keep spaces together


def _read_log_tail(log_path: Path, max_lines: int = 15) -> str:
    """Returns the last few lines of a log file, for error messages."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(log file could not be read)"
    return "\n".join(lines[-max_lines:])


def _kill_process_tree(process: subprocess.Popen) -> None:
    """
    Kills FFmpeg and any child processes.
    On Windows taskkill /T is needed, otherwise ffmpeg.exe can be left running.
    """
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"taskkill failed for FFmpeg pid {process.pid}: {e}")

    # Second attempt in case taskkill was missing or slow.
    try:
        process.kill()
    except OSError:
        pass

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg pid {process.pid} did not exit after being killed.")


FFMPEG_INSTALL_INSTRUCTIONS = """
========================================================================
ERROR: FFmpeg was not found on your system PATH!
FFmpeg is required to extract audio and render video clips.

HOW TO INSTALL ON WINDOWS:
1. Open PowerShell and run:
   winget install Gyan.FFmpeg

2. Alternatively, download "ffmpeg-release-essentials.zip" from:
   https://www.gyan.dev/ffmpeg/builds/
   Extract it (e.g. to C:\\ffmpeg) and add "C:\\ffmpeg\\bin" to your System PATH.

3. Restart your PowerShell terminal and run:
   ffmpeg -version
========================================================================
"""

def find_ffmpeg_executable() -> Optional[str]:
    """Finds the ffmpeg executable on PATH or in standard Windows installation locations."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    winget_packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_packages.exists():
        for candidate in winget_packages.glob("**/ffmpeg.exe"):
            if candidate.is_file():
                return str(candidate)

    c_ffmpeg = Path("C:/ffmpeg/bin/ffmpeg.exe")
    if c_ffmpeg.exists():
        return str(c_ffmpeg)

    return None

def find_ffprobe_executable() -> Optional[str]:
    """Finds the ffprobe executable on PATH or in standard Windows installation locations."""
    found = shutil.which("ffprobe")
    if found:
        return found

    winget_packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_packages.exists():
        for candidate in winget_packages.glob("**/ffprobe.exe"):
            if candidate.is_file():
                return str(candidate)

    c_ffprobe = Path("C:/ffmpeg/bin/ffprobe.exe")
    if c_ffprobe.exists():
        return str(c_ffprobe)

    return None

def verify_ffmpeg_on_startup() -> str:
    """Verifies that FFmpeg is accessible. If missing, logs clear instructions and raises RuntimeError."""
    ffmpeg_path = find_ffmpeg_executable()
    if not ffmpeg_path:
        logger.error(FFMPEG_INSTALL_INSTRUCTIONS)
        print(FFMPEG_INSTALL_INSTRUCTIONS)
        raise RuntimeError(
            "FFmpeg is missing. Install it using 'winget install Gyan.FFmpeg' and restart your terminal."
        )

    logger.info(f"FFmpeg verified at: {ffmpeg_path}")
    return ffmpeg_path

def get_audio_duration_seconds(file_path: Path) -> float:
    """Uses ffprobe to get the exact duration of a video/audio file in seconds."""
    ffprobe_exe = find_ffprobe_executable()
    if not ffprobe_exe:
        raise RuntimeError("ffprobe is not found on your system.")

    cmd = [
        ffprobe_exe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 0.0

def extract_audio_16k_mono(input_video_path: Path, output_wav_path: Path) -> Path:
    """
    Extracts audio from video to a 16 kHz mono WAV file for faster-whisper.
    Uses argument list for subprocess to prevent shell injection.
    """
    ffmpeg_exe = find_ffmpeg_executable()
    if not ffmpeg_exe:
        raise RuntimeError("FFmpeg executable was not found.")

    cmd = [
        ffmpeg_exe,
        "-y",
        "-i", str(input_video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(output_wav_path)
    ]

    logger.info(f"Extracting audio with FFmpeg: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        logger.error(f"FFmpeg audio extraction failed:\n{result.stderr}")
        raise RuntimeError(f"FFmpeg failed to extract audio: {result.stderr.strip()[:300]}")

    if not output_wav_path.exists() or output_wav_path.stat().st_size == 0:
        raise RuntimeError("Extracted audio file is empty or was not created.")

    return output_wav_path

def _run_ffmpeg(cmd: list, log_path: Path, timeout_seconds: float, cwd: Path) -> None:
    """
    Runs FFmpeg safely:
      - arguments are a list, never a shell string
      - stdin is closed (-nostdin + DEVNULL) so FFmpeg cannot wait for input
      - stdout AND stderr go straight into the log file, so a full pipe can't block it
      - if it runs too long, the whole process tree is killed
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"FFmpeg command: {shlex.join(cmd)}")
    logger.info(f"FFmpeg log file: {log_path}")

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"# Command: {shlex.join(cmd)}\n")
        log_file.write(f"# cwd: {cwd}\n")
        log_file.write(f"# Timeout: {timeout_seconds:.0f}s\n\n")
        log_file.flush()

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(cwd),
            creationflags=creationflags,
        )
        log_file.write(f"# pid: {process.pid}\n\n")
        log_file.flush()

        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            message = (
                f"FFmpeg was killed after the {timeout_seconds:.0f}s time limit. "
                f"See {log_path} for details."
            )
            log_file.write(f"\n[cliptor] {message}\n")
            log_file.flush()
            logger.error(message)
            raise RenderError(message) from None

    if process.returncode != 0:
        tail = _read_log_tail(log_path)
        raise RenderError(
            f"FFmpeg failed (exit code {process.returncode}). See {log_path}.\n{tail}"
        )


def render_vertical_clip(
    input_video_path: Path,
    start_time: float,
    end_time: float,
    output_mp4_path: Path,
    subtitle_ass_path: Optional[Path] = None,
    crop_style: str = "blurred_background",
    timeout_seconds: Optional[float] = None,
    log_path: Optional[Path] = None
) -> Path:
    """
    Renders an accurate vertical 9:16 (1080x1920) clip using FFmpeg.
    Styles:
      - 'blurred_background': full video centered over zoomed, blurred copy of itself (default)
      - 'center_crop': cropped to center 1080x1920
    Burns in captions from subtitle_ass_path if provided.
    Encodes with libx264, aac, yuv420p, crf 20, preset veryfast.

    FFmpeg runs with -nostdin, writes stdout+stderr to logs/clip_XX.log,
    and is killed if it passes the time limit (default: max(RENDER_TIMEOUT_SECONDS, 5x clip)).
    """
    ffmpeg_exe = find_ffmpeg_executable()
    if not ffmpeg_exe:
        raise RuntimeError("FFmpeg executable was not found.")

    duration = max(1.0, end_time - start_time)
    work_dir = output_mp4_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    sub_arg = ""
    if subtitle_ass_path and subtitle_ass_path.exists():
        # FFmpeg reads captions from the clip folder (cwd), so it never has to parse
        # "C:", spaces or apostrophes from the full Windows path.
        caption_file = subtitle_ass_path
        if subtitle_ass_path.resolve().parent != work_dir.resolve():
            # Captions live elsewhere: copy them beside the clip under a plain name.
            caption_file = work_dir / f"{subtitle_ass_path.stem}.ass"
            shutil.copyfile(subtitle_ass_path, caption_file)
        sub_arg = f",subtitles={_escape_filter_text(caption_file.name)}"

    cmd = [
        ffmpeg_exe,
        "-y",
        "-nostdin",
        "-ss", f"{start_time:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(input_video_path.resolve()),
    ]

    if crop_style == "center_crop":
        vf_filter = (
            f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920{sub_arg}"
        )
        cmd.extend(["-vf", vf_filter])
    else:
        # Default: blurred_background
        filter_complex = (
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{sub_arg}"
        )
        cmd.extend(["-filter_complex", filter_complex])

    cmd.extend([
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_mp4_path.name)
    ])

    limit = timeout_seconds if timeout_seconds is not None else render_timeout_for(duration)
    active_log = log_path or get_render_log_path(output_mp4_path)

    logger.info(
        f"Rendering clip to {output_mp4_path.name} "
        f"(Style: {crop_style}, Duration: {duration:.1f}s, Timeout: {limit:.0f}s)..."
    )
    _run_ffmpeg(cmd, active_log, limit, cwd=work_dir)

    if not output_mp4_path.exists() or output_mp4_path.stat().st_size == 0:
        raise RenderError(f"Output clip {output_mp4_path.name} was not created or is empty.")

    logger.info(f"Successfully rendered: {output_mp4_path.name} ({output_mp4_path.stat().st_size} bytes)")
    return output_mp4_path

