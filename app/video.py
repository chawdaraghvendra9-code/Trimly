"""
FFmpeg utilities for Cliptor Clip Studio.
Safely calls FFmpeg using argument lists (never raw shell strings).
Handles audio extraction, center cropping, blurred background vertical rendering,
and burning in ASS subtitles.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import LOGS_DIR, RENDER_TIMEOUT_SECONDS
from app.backgrounds import check_background, resolve_background
from app.faces import detect_face_center_x
from app.fonts import copy_font_to_job

logger = logging.getLogger("cliptor")

# "Smart fit" is the default look: a tall crop of the video, 1080 px wide,
# with rounded corners, dropped on a background. The aspect ratio is
# height / width, so 4:5 means 1080 wide and 1350 tall.
DEFAULT_VIDEO_STYLE = "smart_fit"
DEFAULT_SMART_ASPECT = "4:5"
SMART_FIT_RATIOS = {"4:5": 5 / 4, "1:1": 1.0, "3:4": 4 / 3}
SMART_FIT_CORNER_RADIUS = 40
SMART_FIT_SHADOW_PAD = 60      # transparent border that holds the soft shadow
SMART_FIT_SHADOW_OFFSET = 14   # shadow sits this many pixels lower than the video
SMART_FIT_SHADOW_ALPHA = 0.45
SMART_FIT_ZOOM_END = 1.06      # slow punch-in: 1.00 -> 1.06 over the clip

# "Split screen": the main clip on top, a looping background clip below.
SPLIT_TOP_HEIGHT = int(round(1920 * 0.58 / 2)) * 2   # 1114 px, about 58%
SPLIT_SEAM_LINE_HEIGHT = 4
SPLIT_FALLBACK_COLOR = "0x0B1220"                    # used when no background exists
SPLIT_BACKGROUND_MAX = "1280:720"                    # downscale before compositing


class RenderError(RuntimeError):
    """Raised when FFmpeg fails, times out, or produces an empty clip."""


def render_timeout_for(clip_duration: float) -> float:
    """Time limit for one clip: at least RENDER_TIMEOUT_SECONDS, and at least 5x the clip length."""
    return max(RENDER_TIMEOUT_SECONDS, 5.0 * clip_duration)


def get_render_log_path(output_mp4_path: Path) -> Path:
    """Returns logs/clip_01.log for an output file clip_01.mp4."""
    return LOGS_DIR / f"{output_mp4_path.stem}.log"


def escape_filter_path(path: Path) -> str:
    """
    Makes a Windows path safe inside an FFmpeg filter, e.g. subtitles='C\\:/x/clip.ass'.
    Forward slashes, one backslash before the drive colon, quoted so spaces stay together.
    """
    text = str(path.resolve()).replace("\\", "/")
    text = text.replace(":", "\\:")   # escape the drive colon (C:)
    return f"'{text}'"


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def ffmpeg_color(value: str, fallback: str) -> str:
    """'#AABBCC' -> '0xAABBCC'. Invalid user values fall back to a safe color."""
    text = value if isinstance(value, str) else ""
    if not _HEX_COLOR_RE.match(text):
        text = fallback
    return "0x" + text.lstrip("#").upper()


def compute_smart_crop(
    source_width: int,
    source_height: int,
    face_center_x: Optional[float],
    height_ratio: float,
) -> tuple:
    """
    The one static crop window used for the whole clip.

    It is the biggest area with the requested aspect ratio (height / width) that
    fits in the frame, moved horizontally so the face center stays in the middle
    and clamped so it can never leave the picture. Vertically it is centered.
    Returns (width, height, x, y) as even integers.
    """
    src_w = max(2, int(source_width))
    src_h = max(2, int(source_height))
    ratio = height_ratio if height_ratio > 0 else SMART_FIT_RATIOS[DEFAULT_SMART_ASPECT]

    if src_w / src_h > 1.0 / ratio:
        # Source is wider than the window: keep the full height.
        crop_h = src_h
        crop_w = round(src_h / ratio)
    else:
        crop_w = src_w
        crop_h = round(src_w * ratio)

    crop_w = max(2, min(crop_w, src_w))
    crop_h = max(2, min(crop_h, src_h))

    center = face_center_x if face_center_x is not None else src_w / 2.0
    x = round(center - crop_w / 2.0)
    x = max(0, min(x, src_w - crop_w))
    y = round((src_h - crop_h) / 2.0)

    # Even numbers keep the chroma planes happy on yuv420p.
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    x -= x % 2
    y -= y % 2
    return crop_w, crop_h, x, y


def _write_smart_fit_mask(
    path: Path,
    canvas_w: int,
    canvas_h: int,
    block_w: int,
    block_h: int,
    pad: int,
    radius: int,
) -> Optional[Path]:
    """
    Writes a black/white PNG with a white, rounded, anti-aliased rectangle.

    FFmpeg uses it as the alpha channel of the video block, which is how the
    corners get rounded. The file is reused when it already exists.
    """
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        import numpy as np
        import cv2
    except Exception as e:
        logger.warning(f"Rounded corners need OpenCV and numpy ({e}); drawing square corners.")
        return None

    ys, xs = np.mgrid[0:canvas_h, 0:canvas_w].astype(np.float32)
    center_x = pad + (block_w - 1) / 2.0
    center_y = pad + (block_h - 1) / 2.0
    # Distance outside the straight part of the rectangle, per pixel.
    dist_x = np.maximum(np.abs(xs - center_x) - (block_w / 2.0 - radius), 0)
    dist_y = np.maximum(np.abs(ys - center_y) - (block_h / 2.0 - radius), 0)
    # 1 inside, 0 outside, soft over one pixel for smooth edges.
    alpha = np.clip(radius + 0.5 - np.hypot(dist_x, dist_y), 0, 1) * 255.0

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), alpha.astype(np.uint8)):
            return None
    except Exception as e:
        logger.warning(f"Could not write the rounded-corner mask ({e}); drawing square corners.")
        return None
    return path


def _smart_fit_block_chain(
    crop: tuple,
    block_w: int,
    block_h: int,
    zoom: bool,
    duration: float,
    fps: int,
) -> str:
    """Crop the source to the chosen aspect, then fit it to 1080 px wide."""
    crop_w, crop_h, crop_x, crop_y = crop
    chain = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},setsar=1"

    if zoom:
        # Slow punch-in: render a little bigger, then let zoompan shrink the
        # visible window from 1.00 to 1.06 while the output size stays fixed.
        # The frame rate matches the source so the block never stutters.
        big_w = int(round(block_w * SMART_FIT_ZOOM_END / 2)) * 2
        big_h = int(round(block_h * SMART_FIT_ZOOM_END / 2)) * 2
        step = SMART_FIT_ZOOM_END - 1.0
        clip_seconds = max(0.1, duration)
        chain += (
            f",fps={fps},scale={big_w}:{big_h}"
            f",zoompan=z='min(1+{step:.2f}*it/{clip_seconds:.3f},{SMART_FIT_ZOOM_END:.2f})'"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={block_w}x{block_h}:fps={fps},setsar=1"
        )
    else:
        chain += f",scale={block_w}:{block_h}"

    return chain + ",format=rgb24"


def _smart_fit_background(
    background: str,
    solid_color: str,
    gradient_top: str,
    gradient_bottom: str,
    duration: float,
) -> str:
    """The background the block sits on: the same three looks as before."""
    if background == "solid":
        return (
            f"color=c={ffmpeg_color(solid_color, '#000000')}:s=1080x1920:r=30:"
            f"d={duration:.3f}[bg];"
        )
    if background == "gradient":
        return (
            f"gradients=s=1080x1920:c0={ffmpeg_color(gradient_top, '#0F172A')}:"
            f"c1={ffmpeg_color(gradient_bottom, '#1E3A8A')}:"
            f"x0=0:y0=0:x1=0:y1=1920:d={duration:.3f}:r=30[bg];"
        )
    # Default: the blurred copy of the video itself.
    return (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=20:5,setsar=1[bg];"
    )


def build_smart_fit_filter(
    crop: tuple,
    block_w: int,
    block_h: int,
    pad: int,
    canvas_w: int,
    canvas_h: int,
    mask_name: Optional[str],
    background: str,
    solid_color: str,
    gradient_top: str,
    gradient_bottom: str,
    duration: float,
    zoom: bool,
    fps: int,
    tail: str,
) -> str:
    """
    Filter graph for "Smart fit": rounded, shadowed video block over a background.

    Everything is built from numbers and validated colors; no user text and no
    shell string is involved. mask_name is a PNG in the render folder and is
    read as FFmpeg input 1.
    """
    block_chain = _smart_fit_block_chain(crop, block_w, block_h, zoom, duration, fps)
    bg_chain = _smart_fit_background(background, solid_color, gradient_top, gradient_bottom, duration)

    # The canvas is the block plus a transparent border for the shadow. It is
    # placed so the block itself lands exactly in the middle of the frame.
    overlay_x = -pad
    overlay_y = (1920 - canvas_h) // 2
    shadow_y = overlay_y + SMART_FIT_SHADOW_OFFSET

    if not mask_name:
        # No mask available: plain (square) block, still correctly placed.
        return (
            f"[0:v]{block_chain}[blk];"
            f"{bg_chain}"
            f"[bg][blk]overlay={overlay_x}:{overlay_y}:shortest=1{tail}"
        )

    block = f"[0:v]{block_chain},pad={canvas_w}:{canvas_h}:{pad}:{pad}:color=black"

    return (
        # Rounded corners: the mask PNG becomes the alpha channel.
        f"{block}[fgpad];"
        "[1:v]format=gray[rmask];"
        "[fgpad][rmask]alphamerge,split[blk][shsrc];"
        # Soft shadow: the same alpha, heavily blurred, black and see-through.
        "[shsrc]alphaextract,boxblur=20:2[shalpha];"
        f"color=c=black:s={canvas_w}x{canvas_h}:r=30[dark];"
        "[dark][shalpha]alphamerge,"
        f"colorchannelmixer=aa={SMART_FIT_SHADOW_ALPHA}[shadow];"
        f"{bg_chain}"
        f"[bg][shadow]overlay={overlay_x}:{shadow_y}:shortest=1[bgs];"
        f"[bgs][blk]overlay={overlay_x}:{overlay_y}:shortest=1{tail}"
    )


def build_split_filter(
    crop: tuple,
    top_h: int,
    pad: int,
    canvas_w: int,
    canvas_h: int,
    mask_name: Optional[str],
    has_background: bool,
    divider: bool,
    duration: float,
    fps: int,
    tail: str,
) -> str:
    """
    Filter graph for "Split screen": the Smart fit block on top (y=0), a looping
    background clip filling the rest, and an optional thin line at the seam.

    has_background tells us whether FFmpeg input 1 is the background clip, so the
    mask PNG is input 1 without it and input 2 with it. Numbers only, no user text.
    """
    block_chain = _smart_fit_block_chain(crop, 1080, top_h, False, duration, fps)
    mask_label = "[2:v]" if has_background else "[1:v]"
    overlay_x = -pad                      # the pad is off-screen on both sides
    overlay_y = -pad                      # block lands exactly at the top edge
    shadow_y = overlay_y + SMART_FIT_SHADOW_OFFSET

    if has_background:
        # 720p first (cheap), then cover the whole 1080x1920 frame.
        bg_chain = (
            f"[1:v]scale={SPLIT_BACKGROUND_MAX}:force_original_aspect_ratio=decrease"
            ":force_divisible_by=2,setsar=1,"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920[bg];"
        )
    else:
        bg_chain = (
            f"color=c={SPLIT_FALLBACK_COLOR}:s=1080x1920:r=30:d={duration:.3f}[bg];"
        )

    if divider:
        finish = (
            f"drawbox=x=0:y={max(0, top_h - SPLIT_SEAM_LINE_HEIGHT // 2)}:w=iw"
            f":h={SPLIT_SEAM_LINE_HEIGHT}:color=0xFFFFFF@0.35:t=fill{tail}[vout]"
        )
    else:
        finish = f"null{tail}[vout]"

    if not mask_name:
        # No mask: square corners, still correctly placed.
        return (
            f"[0:v]{block_chain}[blk];"
            f"{bg_chain}"
            f"[bg][blk]overlay=0:0:shortest=1[comp];"
            f"[comp]{finish}"
        )

    return (
        f"[0:v]{block_chain},pad={canvas_w}:{canvas_h}:{pad}:{pad}:color=black[fgpad];"
        f"{mask_label}format=gray[rmask];"
        "[fgpad][rmask]alphamerge,split[blk][shsrc];"
        "[shsrc]alphaextract,boxblur=20:2[shalpha];"
        f"color=c=black:s={canvas_w}x{canvas_h}:r=30[dark];"
        "[dark][shalpha]alphamerge,"
        f"colorchannelmixer=aa={SMART_FIT_SHADOW_ALPHA}[shadow];"
        f"{bg_chain}"
        f"[bg][shadow]overlay=0:{shadow_y}:shortest=1[bgs];"
        f"[bgs][blk]overlay={overlay_x}:{overlay_y}:shortest=1[comp];"
        f"[comp]{finish}"
    )


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

class InvalidVideoError(ValueError):
    """Raised when ffprobe says the file is not a usable video."""


def probe_video_file(file_path: Path) -> Dict[str, Any]:
    """
    Asks ffprobe what is inside a file, so we never process garbage.

    Returns {"duration": seconds, "width": px, "height": px, "codec": name}.
    Raises InvalidVideoError with a beginner-friendly message when the file
    is not a readable video.
    """
    ffprobe_exe = find_ffprobe_executable()
    if not ffprobe_exe:
        raise InvalidVideoError("ffprobe was not found, so the video could not be checked.")

    cmd = [
        ffprobe_exe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        raise InvalidVideoError("Checking the video took too long. The file may be damaged.") from None

    if result.returncode != 0:
        raise InvalidVideoError(
            "That file is not a video we can read. It may be damaged, "
            "or the link may point to a web page instead of a video."
        )

    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise InvalidVideoError("That file is not a video we can read.") from None

    # Keep only real picture streams (cover art in MP3s looks like a video stream).
    video_streams = [
        stream
        for stream in info.get("streams", [])
        if stream.get("codec_type") == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ]
    if not video_streams:
        raise InvalidVideoError("That file has no video track, so there is nothing to clip.")

    stream = video_streams[0]

    duration = 0.0
    for raw_value in (info.get("format", {}).get("duration"), stream.get("duration")):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            duration = value
            break

    if duration <= 0:
        raise InvalidVideoError("The length of that video could not be read, so it may be damaged.")

    # Frame rate, used by the "Smart fit" zoom. 0.0 means "unknown".
    fps = 0.0
    for raw_rate in (stream.get("avg_frame_rate"), stream.get("r_frame_rate")):
        try:
            numerator, _, denominator = str(raw_rate).partition("/")
            value = float(numerator) / float(denominator or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if 1.0 <= value <= 240.0:
            fps = value
            break

    return {
        "duration": duration,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": stream.get("codec_name") or "unknown",
        "fps": fps,
    }


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
    log_path: Optional[Path] = None,
    font_filename: Optional[str] = None,
    video_style: str = DEFAULT_VIDEO_STYLE,
    solid_color: str = "#000000",
    gradient_top: str = "#0F172A",
    gradient_bottom: str = "#1E3A8A",
    smart_aspect: str = DEFAULT_SMART_ASPECT,
    smart_zoom: bool = False,
    smart_background: str = "blurred",
    smart_face_x: Optional[float] = None,
    split_background: str = "random",
    split_divider: bool = True,
) -> Path:
    """
    Renders an accurate vertical 9:16 (1080x1920) clip using FFmpeg.

    Video styles:
      - 'smart_fit': tall crop (4:5 by default) that keeps faces in the middle,
        rounded corners, soft shadow, centered on a background (default)
      - 'split': the same Smart fit block on top (~58%), a muted looping clip
        from backgrounds/ filling the rest, optional line at the seam
      - 'blurred': full video centered over a blurred copy of itself
      - 'solid': video fitted inside 1080x1920, centered on a solid color
      - 'gradient': same layout over a two-color vertical gradient
    The hidden 'center_crop' code still works when video_style is 'blurred'.

    Burns in captions from subtitle_ass_path if provided.
    font_filename copies only the font in use into <job>/fonts/ and passes
    fontsdir=fonts, so libass finds it without any Windows path escaping.
    smart_face_x skips face detection (tests pass a known center).
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
        # FFmpeg runs with cwd=work_dir, so a bare file name works and avoids
        # the drive colon and space escaping problems on Windows.
        if subtitle_ass_path.resolve().parent == work_dir.resolve():
            subtitle_arg = subtitle_ass_path.name
        else:
            subtitle_arg = escape_filter_path(subtitle_ass_path)

        sub_arg = f",subtitles={subtitle_arg}"
        if font_filename:
            copied = copy_font_to_job(font_filename, work_dir)
            if copied is not None:
                sub_arg += ":fontsdir=fonts"
            else:
                logger.warning(f"Caption font {font_filename} was not found; using ffmpeg defaults.")

    tail = sub_arg

    cmd = [
        ffmpeg_exe,
        "-y",
        "-nostdin",
        # Accurate seek: FFmpeg seeks to the previous keyframe, then decodes up to
        # the exact timestamp. Together with the libx264 re-encode below this is
        # frame-accurate (no rough stream copy).
        "-accurate_seek",
        "-ss", f"{start_time:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(input_video_path.resolve()),
    ]

    if video_style == "smart_fit":
        try:
            info = probe_video_file(input_video_path)
        except InvalidVideoError as e:
            raise RenderError(str(e)) from None

        ratio = SMART_FIT_RATIOS.get(smart_aspect, SMART_FIT_RATIOS[DEFAULT_SMART_ASPECT])
        if smart_face_x is None:
            # One static crop window: faces are found on a few frames, then the
            # median center is used for the whole clip (no jitter).
            smart_face_x = detect_face_center_x(input_video_path, start_time, end_time)
        crop = compute_smart_crop(info["width"], info["height"], smart_face_x, ratio)

        # Keep the block at the source frame rate; 30 is a safe fallback.
        source_fps = 30 if not info.get("fps") else max(15, min(60, round(info["fps"])))
        block_h = int(round(1080 * ratio / 2)) * 2
        pad = SMART_FIT_SHADOW_PAD
        canvas_w, canvas_h = 1080 + 2 * pad, block_h + 2 * pad
        mask_path = _write_smart_fit_mask(
            work_dir / f"smartfit_mask_{canvas_w}x{canvas_h}.png",
            canvas_w,
            canvas_h,
            1080,
            block_h,
            pad,
            SMART_FIT_CORNER_RADIUS,
        )
        mask_name = mask_path.name if mask_path else None

        logger.info(
            f"Smart fit: aspect {smart_aspect}, crop {crop[0]}x{crop[1]} at "
            f"x={crop[2]}, y={crop[3]}, zoom={smart_zoom}, background={smart_background}"
        )

        filter_complex = build_smart_fit_filter(
            crop=crop,
            block_w=1080,
            block_h=block_h,
            pad=pad,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            mask_name=mask_name,
            background=smart_background,
            solid_color=solid_color,
            gradient_top=gradient_top,
            gradient_bottom=gradient_bottom,
            duration=duration,
            zoom=smart_zoom,
            fps=source_fps,
            tail=tail,
        )
        if mask_name:
            # A still PNG, looped so it covers the whole clip.
            cmd.extend(["-loop", "1", "-framerate", "30", "-i", mask_name])
        cmd.extend(["-filter_complex", filter_complex])
    elif video_style == "split":
        try:
            info = probe_video_file(input_video_path)
        except InvalidVideoError as e:
            raise RenderError(str(e)) from None

        top_h = SPLIT_TOP_HEIGHT
        ratio = top_h / 1080.0
        if smart_face_x is None:
            smart_face_x = detect_face_center_x(input_video_path, start_time, end_time)
        crop = compute_smart_crop(info["width"], info["height"], smart_face_x, ratio)
        source_fps = 30 if not info.get("fps") else max(15, min(60, round(info["fps"])))

        pad = SMART_FIT_SHADOW_PAD
        canvas_w, canvas_h = 1080 + 2 * pad, top_h + 2 * pad
        mask_path = _write_smart_fit_mask(
            work_dir / f"split_mask_{canvas_w}x{canvas_h}.png",
            canvas_w,
            canvas_h,
            1080,
            top_h,
            pad,
            SMART_FIT_CORNER_RADIUS,
        )
        mask_name = mask_path.name if mask_path else None

        # The background clip comes from backgrounds/ (or a dark fallback).
        background_path = resolve_background(split_background)
        if background_path is None:
            logger.warning("Split screen: no background clip available, using a dark background.")
        else:
            problem = check_background(background_path)
            if problem:
                logger.warning(f"Split screen: ignoring background {background_path.name} ({problem}).")
                background_path = None

        logger.info(
            f"Split screen: crop {crop[0]}x{crop[1]} at x={crop[2]}, y={crop[3]}, "
            f"background={background_path.name if background_path else 'dark fallback'}, "
            f"divider={split_divider}"
        )

        filter_complex = build_split_filter(
            crop=crop,
            top_h=top_h,
            pad=pad,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            mask_name=mask_name,
            has_background=background_path is not None,
            divider=split_divider,
            duration=duration,
            fps=source_fps,
            tail=tail,
        )
        if background_path is not None:
            # Loop forever, read only the clip length, and never map its audio.
            cmd.extend([
                "-stream_loop", "-1",
                "-t", f"{duration:.3f}",
                "-i", str(background_path.resolve()),
            ])
        if mask_name:
            cmd.extend(["-loop", "1", "-framerate", "30", "-i", mask_name])
        cmd.extend(["-filter_complex", filter_complex])
    elif video_style == "solid":
        solid = ffmpeg_color(solid_color, "#000000")
        vf_filter = (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:0:(1920-ih)/2:color={solid}{tail}"
        )
        cmd.extend(["-vf", vf_filter])
    elif video_style == "gradient":
        top = ffmpeg_color(gradient_top, "#0F172A")
        bottom = ffmpeg_color(gradient_bottom, "#1E3A8A")
        filter_complex = (
            f"gradients=s=1080x1920:c0={top}:c1={bottom}:x0=0:y0=0:x1=0:y1=1920:"
            f"d={duration:.3f}:r=30[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1{tail}"
        )
        cmd.extend(["-filter_complex", filter_complex])
    elif crop_style == "center_crop":
        # Kept for the old API even though the UI always uses 'blurred'.
        vf_filter = (
            f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920{tail}"
        )
        cmd.extend(["-vf", vf_filter])
    else:
        # Default: blurred_background
        filter_complex = (
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{tail}"
        )
        cmd.extend(["-filter_complex", filter_complex])

    # zoompan can add one frame at the end; trim the clip back to its length.
    trim_args = ["-t", f"{duration:.3f}"] if (video_style == "smart_fit" and smart_zoom) else []
    # Split screen builds labelled output, so the streams must be mapped by hand.
    map_args = ["-map", "[vout]", "-map", "0:a"] if video_style == "split" else []

    cmd.extend([
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        # 50 ms audio fade in/out so clips do not click at the edges
        "-af", f"afade=t=in:st=0:d=0.05,afade=t=out:st={max(0.0, duration - 0.05):.3f}:d=0.05",
        "-c:a", "aac",
        "-b:a", "192k",
        *map_args,
        *trim_args,
        str(output_mp4_path.name)
    ])

    limit = timeout_seconds if timeout_seconds is not None else render_timeout_for(duration)
    active_log = log_path or get_render_log_path(output_mp4_path)

    logger.info(
        f"Rendering clip to {output_mp4_path.name} "
        f"(Video style: {video_style}, crop: {crop_style}, "
        f"Duration: {duration:.1f}s, Timeout: {limit:.0f}s)..."
    )
    _run_ffmpeg(cmd, active_log, limit, cwd=work_dir)

    if not output_mp4_path.exists() or output_mp4_path.stat().st_size == 0:
        raise RenderError(f"Output clip {output_mp4_path.name} was not created or is empty.")

    logger.info(f"Successfully rendered: {output_mp4_path.name} ({output_mp4_path.stat().st_size} bytes)")
    return output_mp4_path

