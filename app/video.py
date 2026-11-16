"""
FFmpeg utilities for Cliptor Clip Studio.
Safely calls FFmpeg using argument lists (never raw shell strings).
Handles audio extraction, center cropping, blurred background vertical rendering,
and burning in ASS subtitles.
"""
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cliptor")

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

def render_vertical_clip(
    input_video_path: Path,
    start_time: float,
    end_time: float,
    output_mp4_path: Path,
    subtitle_ass_path: Optional[Path] = None,
    crop_style: str = "blurred_background"
) -> Path:
    """
    Renders an accurate vertical 9:16 (1080x1920) clip using FFmpeg.
    Styles:
      - 'blurred_background': full video centered over zoomed, blurred copy of itself (default)
      - 'center_crop': cropped to center 1080x1920
    Burns in captions from subtitle_ass_path if provided.
    Encodes with libx264, aac, yuv420p, crf 20, preset veryfast.
    """
    ffmpeg_exe = find_ffmpeg_executable()
    if not ffmpeg_exe:
        raise RuntimeError("FFmpeg executable was not found.")

    duration = max(1.0, end_time - start_time)
    work_dir = output_mp4_path.parent
    sub_arg = ""
    if subtitle_ass_path and subtitle_ass_path.exists():
        # Pass filename relative to work_dir so drive-letters and colons don't interfere
        sub_arg = f",subtitles={subtitle_ass_path.name}"

    cmd = [
        ffmpeg_exe,
        "-y",
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

    logger.info(f"Rendering clip to {output_mp4_path.name} (Style: {crop_style}, Duration: {duration:.1f}s)...")
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(work_dir)
    )

    if res.returncode != 0:
        logger.error(f"FFmpeg render error for {output_mp4_path.name}:\n{res.stderr}")
        raise RuntimeError(f"FFmpeg failed to render clip: {res.stderr.strip()[:400]}")

    if not output_mp4_path.exists() or output_mp4_path.stat().st_size == 0:
        raise RuntimeError(f"Output clip {output_mp4_path.name} was not created or is empty.")

    logger.info(f"Successfully rendered: {output_mp4_path.name} ({output_mp4_path.stat().st_size} bytes)")
    return output_mp4_path
