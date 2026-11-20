"""
Record a short window from a live stream, then let the normal pipeline clip it.

How it works:
  1. The user pastes a live link and picks a length (default 90 seconds).
  2. ffmpeg connects to the stream and records exactly that many seconds.
  3. The saved MP4 becomes the job's input video: transcribe, suggest, render.

Supported out of the box: direct HTTP(S) live streams such as HLS (.m3u8),
MPEG-DASH (.mpd) or MPEG-TS. YouTube and Twitch live *pages* need
ENABLE_YOUTUBE_IMPORT=true; there yt-dlp is asked for the real HLS address.

Only http:// and https:// links are accepted, and the same public-address
check as normal link imports blocks localhost and private networks.
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from app.config import ENABLE_YOUTUBE_IMPORT
from app.downloader import (
    LinkError,
    build_ytdlp_options,
    friendly_ytdlp_error,
    validate_public_url,
    _run_with_deadline,
)
from app.logger import logger
from app.video import InvalidVideoError, find_ffmpeg_executable, probe_video_file

MIN_CAPTURE_SECONDS = 20
MAX_CAPTURE_SECONDS = 600
DEFAULT_CAPTURE_SECONDS = 90

# Sites where the pasted link is a web page, not a direct stream address.
LIVE_PAGE_HOSTS = {"youtube.com", "youtu.be", "twitch.tv"}

# Give ffmpeg extra time to connect and flush the last seconds.
CAPTURE_GRACE_SECONDS = 120


class LiveStreamError(Exception):
    """A live stream problem with a message that is safe to show in the UI."""


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_live_page(url: str) -> bool:
    """True for YouTube/Twitch page links that need yt-dlp to find the stream."""
    host = _host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in LIVE_PAGE_HOSTS)


def validate_live_url(raw_url: str) -> str:
    """Checks the scheme and public address before a job is created."""
    url = (raw_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise LiveStreamError(
            "Only http:// and https:// live links are supported. "
            "Paste the direct HLS (.m3u8) address of the stream."
        )
    if is_live_page(url) and not ENABLE_YOUTUBE_IMPORT:
        raise LiveStreamError(
            "YouTube and Twitch live pages are turned off on this copy. "
            "Use a direct HLS (.m3u8) link, or set ENABLE_YOUTUBE_IMPORT=true and restart."
        )
    try:
        validate_public_url(url)
    except LinkError as error:
        raise LiveStreamError(str(error)) from error
    return url


def resolve_live_source(url: str, work_dir: Path) -> str:
    """
    Returns a direct stream address. For a YouTube/Twitch live page, ask
    yt-dlp for the current HLS manifest. Direct links are returned unchanged.
    """
    if not is_live_page(url):
        return url

    options = build_ytdlp_options(work_dir)
    options.update(
        {
            "skip_download": True,
            # Prefer HLS; ffmpeg records it more reliably than a web page.
            "format": "best[protocol^=m3u8]/best[protocol^=http]/best",
        }
    )
    holder = {}

    def work() -> None:
        import yt_dlp

        with yt_dlp.YoutubeDL(options) as ydl:
            holder["info"] = ydl.extract_info(url, download=False) or {}

    try:
        # _run_with_deadline is shared with the normal downloader so a lookup
        # can never hang the job thread forever.
        _run_with_deadline(
            work,
            120,
            "Looking up the live stream took too long. Check the link and try again.",
        )
    except LiveStreamError:
        raise
    except Exception as error:
        raise LiveStreamError(friendly_ytdlp_error(str(error))) from error

    info = holder.get("info") or {}
    if info.get("entries"):
        # A channel URL can expand to several items; take the first one.
        info = next(iter(info["entries"]), {}) or {}

    if info.get("is_live") is False:
        raise LiveStreamError(
            "That stream is not live right now. Use 'Import from link' for a finished video."
        )

    manifest = info.get("manifest_url") or info.get("url")
    if not manifest:
        for fmt in info.get("formats") or []:
            candidate = fmt.get("url") or ""
            if candidate.startswith(("http://", "https://")):
                manifest = candidate
                break
    if not manifest or not str(manifest).startswith(("http://", "https://")):
        raise LiveStreamError(
            "No playable stream address was found. Make sure the stream is live and public."
        )

    try:
        validate_public_url(str(manifest))
    except LinkError as error:
        raise LiveStreamError(str(error)) from error
    return str(manifest)


def build_capture_command(
    ffmpeg: str,
    url: str,
    seconds: int,
    output: Path,
    encode: bool = False,
) -> list[str]:
    """
    The exact ffmpeg arguments. A list, never a shell string, so URLs and
    paths with special characters are passed through untouched.
    """
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "warning",
        "-rw_timeout",
        "15000000",
        "-i",
        url,
        "-t",
        str(int(seconds)),
    ]
    if encode:
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
        ]
    else:
        # Fastest and keeps the original quality.
        command += ["-c", "copy"]
    command += [
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output),
    ]
    return command


def _stop_process(process: subprocess.Popen) -> None:
    """Stops ffmpeg and waits briefly, so no recorder is left running."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg did not exit after the live capture was stopped.")


def _capture_once(
    ffmpeg: str,
    url: str,
    seconds: int,
    output: Path,
    log_path: Path,
    encode: bool,
    progress_cb: Optional[Callable[[int], None]],
) -> bool:
    """Runs one ffmpeg attempt. Returns True when the file is a valid video."""
    command = build_capture_command(ffmpeg, url, seconds, output, encode=encode)

    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(
            f"\n=== Live capture ({'encode' if encode else 'copy'}) "
            f"for {seconds}s ===\n"
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log_file,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # Read progress on a helper thread so the deadline check below runs.
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def read_output() -> None:
            for line in process.stdout or []:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read_output, daemon=True, name="live-progress").start()

        started = time.monotonic()
        deadline = started + seconds + CAPTURE_GRACE_SECONDS
        last_percent = -1

        try:
            while True:
                try:
                    line = lines.get(timeout=1.0)
                except queue.Empty:
                    line = ""

                if line is None:
                    break
                if line:
                    key, _, value = line.strip().partition("=")
                    if key in {"out_time_us", "out_time_ms"}:
                        try:
                            micros = int(value)
                        except ValueError:
                            micros = 0
                        percent = min(99, max(0, int((micros / 1_000_000) / seconds * 100)))
                        if percent != last_percent:
                            last_percent = percent
                            if progress_cb:
                                progress_cb(percent)

                if process.poll() is not None and lines.empty():
                    break
                if time.monotonic() > deadline:
                    _stop_process(process)
                    raise LiveStreamError(
                        "Recording took too long and was stopped. Try a shorter length."
                    )
        finally:
            if process.poll() is None:
                _stop_process(process)
            process.wait()
            if process.stdout:
                process.stdout.close()

    if process.returncode != 0:
        logger.warning(f"Live capture ffmpeg exited with code {process.returncode}.")
        return False
    if not output.is_file() or output.stat().st_size < 1024:
        return False

    try:
        # A real video must have a readable video stream.
        info = probe_video_file(output)
    except InvalidVideoError:
        return False
    return bool(info.get("width") and info.get("height"))


def capture_live_stream(
    url: str,
    output: Path,
    seconds: int,
    log_path: Path,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> Path:
    """
    Records ``seconds`` of a live stream into ``output``.
    Tries a fast stream copy first; if the codecs do not fit MP4, it records
    once more with H.264/AAC so the rest of the pipeline always has a file.
    """
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        raise LiveStreamError(
            "FFmpeg was not found, so the live stream cannot be recorded. "
            "Run: winget install Gyan.FFmpeg  (then open a new terminal)."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Recording live stream for {seconds}s to {output.name}")

    if _capture_once(ffmpeg, url, seconds, output, log_path, False, progress_cb):
        return output

    logger.warning("Live stream copy failed; trying again with H.264/AAC encoding.")
    if progress_cb:
        progress_cb(0)
    if _capture_once(ffmpeg, url, seconds, output, log_path, True, progress_cb):
        return output

    try:
        output.unlink()
    except OSError:
        pass
    raise LiveStreamError(
        f"The live stream could not be recorded. Open {log_path.name} in the logs "
        "folder for the ffmpeg details."
    )


def friendly_live_error(error: Exception) -> str:
    """One short sentence for the studio page."""
    if isinstance(error, (LiveStreamError, LinkError)):
        return str(error)
    return (
        "The live stream could not be recorded. Check that it is still live and "
        "public, then try a shorter capture."
    )
