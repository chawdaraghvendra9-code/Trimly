"""
Job management and background execution for Cliptor Clip Studio.
Processes jobs one at a time: audio extraction -> transcription -> highlight suggestions -> rendering vertical clips.
Includes detailed stage-by-stage timing instrumentation and benchmark metrics.
"""
import json
import os
import uuid
import time
import threading
import queue
import traceback
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from app.config import JOBS_DIR, WHISPER_MODEL, DEFAULT_CROP_STYLE, MAX_VIDEO_MINUTES
from app.logger import logger
from app.downloader import (
    LinkError,
    cleanup_partial_download,
    download_from_link,
    friendly_link_error,
)
from app.video import (
    extract_audio_16k_mono,
    find_ffmpeg_executable,
    FFMPEG_INSTALL_INSTRUCTIONS,
    render_vertical_clip,
    get_audio_duration_seconds,
    probe_video_file,
    InvalidVideoError,
)
from app.transcription import (
    transcribe_audio_file,
    NoSpeechFoundError,
    ModelDownloadError,
    TranscriptionError
)
from app.highlights import pick_highlights
from app.snapping import snap_to_word_boundaries
from app.subtitles import generate_ass_subtitles_for_clip
from app.fonts import DEFAULT_FONT_STYLE, get_font_style

# Split screen: captions sit just above the seam (about 53-58% of the frame).
SPLIT_CAPTION_MARGIN_V = 806

# Queue for background jobs
_job_queue: queue.Queue = queue.Queue()
_worker_thread: Optional[threading.Thread] = None

def get_job_dir(job_id: str) -> Path:
    """Returns the Path to the job's directory."""
    return JOBS_DIR / job_id

def get_job_file(job_id: str) -> Path:
    """Returns the Path to the job.json file."""
    return get_job_dir(job_id) / "job.json"

def get_transcript_file(job_id: str) -> Path:
    """Returns the Path to the transcript.json file."""
    return get_job_dir(job_id) / "transcript.json"

def get_candidates_file(job_id: str) -> Path:
    """Returns the Path to the candidates.json file."""
    return get_job_dir(job_id) / "candidates.json"

def load_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Loads job data from job.json. Returns None if not found."""
    job_file = get_job_file(job_id)
    if not job_file.exists():
        return None
    try:
        with open(job_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading {job_file}: {e}")
        return None

def save_job(job_id: str, data: Dict[str, Any]) -> None:
    """Saves updated job data to job.json.
    Writes to a temp file then swaps it in, so the polling UI never reads a half-written file."""
    job_file = get_job_file(job_id)
    temp_file = job_file.with_suffix(".json.tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, job_file)
    except Exception as e:
        logger.error(f"Error saving {job_file}: {e}")

def update_job_status(job_id: str, status: str, message: str, percent: int, error: Optional[str] = None) -> None:
    """Updates job status, progress percent, and error details."""
    data = load_job(job_id)
    if data:
        data["status"] = status
        data["progress_message"] = message
        data["progress_percent"] = percent
        if error is not None:
            data["error"] = error
        save_job(job_id, data)

def create_job(
    original_filename: str,
    file_size_bytes: int,
    crop_style: str = DEFAULT_CROP_STYLE,
    source_url: Optional[str] = None,
    font_style: str = DEFAULT_FONT_STYLE,
    video_style: str = "smart_fit",
    solid_color: str = "#000000",
    gradient_top: str = "#0F172A",
    gradient_bottom: str = "#1E3A8A",
    progress_bar: bool = False,
    progress_color: str = "#38BDF8",
    hook_enabled: bool = False,
    hook_duration: int = 3,
    smart_aspect: str = "4:5",
    smart_zoom: bool = False,
    smart_background: str = "blurred",
    split_background: str = "random",
    split_divider: bool = True,
) -> Dict[str, Any]:
    """
    Creates a new job with a unique folder and an initial job.json file.
    When source_url is given the job is an "import from link" job.
    """
    job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    job_dir = get_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    job_data: Dict[str, Any] = {
        "job_id": job_id,
        "original_filename": original_filename,
        "file_size_bytes": file_size_bytes,
        "crop_style": crop_style,
        "font_style": font_style,
        "video_style": video_style,
        "solid_color": solid_color,
        "gradient_top": gradient_top,
        "gradient_bottom": gradient_bottom,
        "progress_bar": progress_bar,
        "progress_color": progress_color,
        "hook_enabled": hook_enabled,
        "hook_duration": hook_duration,
        "smart_aspect": smart_aspect,
        "smart_zoom": smart_zoom,
        "smart_background": smart_background,
        "split_background": split_background,
        "split_divider": split_divider,
        "source": "link" if source_url else "upload",
        "source_url": source_url,
        # A link job starts as "downloading"; an upload starts as "uploaded".
        "status": "downloading" if source_url else "uploaded",
        "progress_message": (
            "Waiting to start the download..." if source_url
            else "Video uploaded. Queued for processing..."
        ),
        "progress_percent": 0 if source_url else 5,
        "duration_seconds": 0.0,
        "created_at": time.time(),
        "error": None,
        "candidates": [],
        "rendered_clips": [],
        "timing": {}
    }

    save_job(job_id, job_data)
    return job_data

def _save_candidates_progress(job_id: str, candidates: List[Dict[str, Any]]) -> None:
    """Writes candidate statuses into job.json so the polling UI can show each clip as it finishes."""
    data = load_job(job_id)
    if data:
        data["candidates"] = candidates
        save_job(job_id, data)

def start_link_import(job_id: str, url: str) -> None:
    """
    Starts downloading a video link in the background.
    When the file is downloaded and checked, the normal pipeline is queued.
    """
    thread = threading.Thread(
        target=_run_link_import,
        args=(job_id, url),
        daemon=True,
        name=f"import-{job_id}",
    )
    thread.start()
    logger.info(f"[{job_id}] Link import started.")


def _run_link_import(job_id: str, url: str) -> None:
    """Background worker: download -> ffprobe check -> duration limit -> normal pipeline."""
    job_dir = get_job_dir(job_id)
    try:
        update_job_status(job_id, "downloading", "Connecting to the link...", 2)

        def on_progress(downloaded: int, total: int) -> None:
            if total > 0:
                percent = min(95, max(2, int(downloaded * 100 / total)))
                message = (
                    f"Downloading video... {percent}% "
                    f"({downloaded // (1024 * 1024)} of {total // (1024 * 1024)} MB)"
                )
            else:
                # Unknown size: creep forward so the bar still shows activity.
                percent = min(95, 2 + downloaded // (1024 * 1024))
                message = f"Downloading video... {downloaded / (1024 * 1024):.1f} MB"
            update_job_status(job_id, "downloading", message, percent)

        def on_stage(message: str, percent: int) -> None:
            """Streaming imports send their real stage (info, download %, merge...)."""
            update_job_status(job_id, "downloading", message, percent)

        result = download_from_link(
            url,
            job_dir,
            filename_stem="input",
            progress_cb=on_progress,
            stage_cb=on_stage,
        )
        provider = result.provider

        # Make sure it really is a video before spending minutes transcribing it.
        update_job_status(job_id, "downloading", "Checking file...", 96)
        probe_started = time.monotonic()
        info = probe_video_file(result.path)
        logger.info(f"[{job_id}] ffprobe check took {time.monotonic() - probe_started:.2f}s")

        if info["duration"] > MAX_VIDEO_MINUTES * 60:
            minutes = info["duration"] / 60.0
            raise LinkError(
                f"This video is {minutes:.1f} minutes long. The limit is "
                f"{MAX_VIDEO_MINUTES} minutes, so please trim it and import again."
            )

        # Save what we learned, then hand the job to the normal pipeline.
        data = load_job(job_id) or {}
        data["file_size_bytes"] = result.size_bytes
        data["duration_seconds"] = round(info["duration"], 2)
        data["video_width"] = info["width"]
        data["video_height"] = info["height"]
        data["source"] = "link"
        data["source_url"] = url
        data["resolved_url"] = result.final_url
        data["import_provider"] = provider
        data["error"] = None
        save_job(job_id, data)
        update_job_status(job_id, "uploaded", "Download finished. Queued for processing...", 5)

        logger.info(
            f"[{job_id}] Link import ready: {result.path.name} "
            f"({info['width']}x{info['height']}, {info['duration']:.1f}s, "
            f"{info['codec']}, via {provider})"
        )
        enqueue_job(job_id)

    except Exception as e:
        # Leave nothing broken behind for the pipeline to pick up.
        cleanup_partial_download(job_dir)
        if isinstance(e, (LinkError, InvalidVideoError)):
            message = str(e)
        else:
            message = friendly_link_error(e)
        logger.error(f"[{job_id}] Import from link failed: {e}")
        update_job_status(job_id, "error", "Import failed", 0, error=message)


def _check_clip_duration(clip_path: Path, expected_seconds: float) -> float:
    """Measures the real rendered clip length with ffprobe and logs expected vs actual."""
    try:
        actual = get_audio_duration_seconds(clip_path)
    except Exception as e:
        logger.warning(f"Could not measure {clip_path.name} with ffprobe: {e}")
        return 0.0
    logger.info(
        f"Duration check [{clip_path.name}]: expected {expected_seconds:.2f}s, "
        f"actual {actual:.2f}s, difference {actual - expected_seconds:+.2f}s"
    )
    return actual

def _video_render_options(job_data: Dict[str, Any]) -> Dict[str, Any]:
    """Project-wide video style settings, with safe defaults."""
    return {
        "video_style": job_data.get("video_style", "smart_fit"),
        "solid_color": job_data.get("solid_color", "#000000"),
        "gradient_top": job_data.get("gradient_top", "#0F172A"),
        "gradient_bottom": job_data.get("gradient_bottom", "#1E3A8A"),
        "smart_aspect": job_data.get("smart_aspect", "4:5"),
        "smart_zoom": bool(job_data.get("smart_zoom", False)),
        "smart_background": job_data.get("smart_background", "blurred"),
        "split_background": job_data.get("split_background", "random"),
        "split_divider": bool(job_data.get("split_divider", True)),
    }


def _caption_style_for(font_style: Dict[str, Any], job_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    In Split screen the captions move up to the seam (about 53-58% of the
    height), so they never sit on the speaker's face in the top block.
    """
    if job_data.get("video_style") == "split":
        moved = dict(font_style)
        moved["margin_v"] = SPLIT_CAPTION_MARGIN_V
        return moved
    return font_style


def _hook_for_candidate(cand: Dict[str, Any], job_data: Dict[str, Any]) -> Tuple[Optional[str], int]:
    """The hook is off by default; when on, the AI title is the pre-filled text."""
    duration = max(1, min(6, int(job_data.get("hook_duration", 3))))
    if not job_data.get("hook_enabled", False):
        return None, duration
    # An explicitly cleared hook box stays empty; otherwise use the AI title.
    if "hook_title" in cand:
        title = str(cand.get("hook_title") or "").strip()
    else:
        title = str(cand.get("title") or "").strip()
    return (title or None), duration


def render_single_candidate(
    job_id: str,
    clip_id: str,
    new_start: Optional[float] = None,
    new_end: Optional[float] = None,
    new_crop_style: Optional[str] = None,
    new_font_style: Optional[str] = None,
    new_hook_title: Optional[str] = None
) -> Dict[str, Any]:
    """
    Renders or re-renders an individual clip candidate with timing metrics.
    Supports nudge/trim adjustments and crop style switching.
    """
    t_start = time.perf_counter()
    job_data = load_job(job_id)
    if not job_data:
        raise ValueError(f"Job {job_id} not found.")

    job_dir = get_job_dir(job_id)
    video_files = list(job_dir.glob("input.*"))
    if not video_files:
        raise ValueError("Source video not found in job.")
    input_video = video_files[0]

    transcript_file = get_transcript_file(job_id)
    if not transcript_file.exists():
        raise ValueError("Transcript not found for this job.")
    with open(transcript_file, "r", encoding="utf-8") as f:
        transcript_data = json.load(f)
    segments = transcript_data.get("segments", [])

    candidates = job_data.get("candidates", [])
    target_cand = None
    for c in candidates:
        if c.get("id") == clip_id:
            target_cand = c
            break

    if not target_cand:
        raise ValueError(f"Candidate clip {clip_id} not found in job.")

    # Apply trimmed times if provided
    if new_start is not None:
        target_cand["start"] = max(0.0, round(float(new_start), 2))
    if new_end is not None:
        target_cand["end"] = max(target_cand["start"] + 1.0, round(float(new_end), 2))

    target_cand["duration"] = round(target_cand["end"] - target_cand["start"], 2)

    crop_style = new_crop_style or job_data.get("crop_style", DEFAULT_CROP_STYLE)
    if new_crop_style:
        job_data["crop_style"] = crop_style

    # Caption font: a single-clip re-render can pick a new one, otherwise the
    # project-wide choice saved in job.json is used.
    font_style_key = new_font_style or job_data.get("font_style", DEFAULT_FONT_STYLE)
    if new_font_style:
        job_data["font_style"] = font_style_key
    font_style = get_font_style(font_style_key)

    # A per-clip hook title can be edited before re-rendering this clip.
    if new_hook_title is not None:
        target_cand["hook_title"] = str(new_hook_title).strip()

    hook_title, hook_duration = _hook_for_candidate(target_cand, job_data)

    # Generate ASS subtitles for this clip
    ass_path = job_dir / f"{clip_id}.ass"
    generate_ass_subtitles_for_clip(
        transcript_segments=segments,
        clip_start=target_cand["start"],
        clip_end=target_cand["end"],
        output_ass_path=ass_path,
        font_style=_caption_style_for(font_style, job_data),
        hook_title=hook_title,
        hook_duration=hook_duration,
        progress_bar=bool(job_data.get("progress_bar", False)),
        progress_color=job_data.get("progress_color", "#38BDF8"),
    )

    # Render MP4
    output_mp4 = job_dir / f"{clip_id}.mp4"
    render_vertical_clip(
        input_video_path=input_video,
        start_time=target_cand["start"],
        end_time=target_cand["end"],
        output_mp4_path=output_mp4,
        subtitle_ass_path=ass_path,
        crop_style=crop_style,
        font_filename=font_style.get("filename"),
        **_video_render_options(job_data),
    )

    # Log expected vs real duration (ffprobe)
    _check_clip_duration(output_mp4, target_cand["duration"])

    t_render = time.perf_counter() - t_start
    logger.info(f"[{job_id}] Re-rendering {clip_id} ({target_cand['duration']}s clip) completed in {t_render:.2f}s")

    # Update candidate metadata with cache-busting timestamp and render duration
    target_cand["rendered_file"] = f"{clip_id}.mp4"
    target_cand["rendered_url"] = f"/api/jobs/{job_id}/clips/{clip_id}.mp4?t={int(time.time())}"
    target_cand["render_status"] = "completed"
    target_cand["last_render_seconds"] = round(t_render, 2)

    save_job(job_id, job_data)
    candidates_file = get_candidates_file(job_id)
    if candidates_file.exists():
        with open(candidates_file, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2)

    return target_cand

def process_single_job(job_id: str) -> None:
    """
    Background worker function that extracts audio, transcribes,
    selects highlights, and renders vertical clips with captions.
    Measures and logs exact timing for every single stage.
    """
    t_pipeline_start = time.perf_counter()
    logger.info(f"--- Processing Job: {job_id} ---")
    job_dir = get_job_dir(job_id)

    try:
        # Step 1: Verify FFmpeg
        ffmpeg_exe = find_ffmpeg_executable()
        if not ffmpeg_exe:
            err_msg = "FFmpeg is missing on your system. Please install it with: winget install Gyan.FFmpeg"
            logger.error(FFMPEG_INSTALL_INSTRUCTIONS)
            update_job_status(job_id, "error", "FFmpeg missing", 0, error=err_msg)
            return

        # Step 2: Locate uploaded video & get duration
        video_files = list(job_dir.glob("input.*"))
        if not video_files:
            err_msg = "Uploaded video file could not be found in the job folder."
            logger.error(f"[{job_id}] {err_msg}")
            update_job_status(job_id, "error", "File not found", 0, error=err_msg)
            return

        input_video = video_files[0]
        video_duration_sec = get_audio_duration_seconds(input_video)
        video_duration_min = round(video_duration_sec / 60.0, 2)
        logger.info(f"[{job_id}] Source Video: {input_video.name} | Length: {video_duration_sec:.1f}s ({video_duration_min} minutes)")

        # Step 3: Extract audio (with timing)
        update_job_status(job_id, "transcribing", "Extracting audio from video...", 10)
        audio_wav = job_dir / "audio.wav"

        t_audio_start = time.perf_counter()
        extract_audio_16k_mono(input_video, audio_wav)
        t_audio = time.perf_counter() - t_audio_start
        logger.info(f"[{job_id}] [STAGE 1/4] Audio extraction finished in {t_audio:.2f}s")

        # Step 4: Transcribe with Whisper (with timing)
        def progress_cb(msg: str, pct: int):
            update_job_status(job_id, "transcribing", msg, pct)

        t_transcribe_start = time.perf_counter()
        transcript_data = transcribe_audio_file(
            audio_path=audio_wav,
            model_size=WHISPER_MODEL,
            progress_callback=progress_cb
        )
        t_transcribe = time.perf_counter() - t_transcribe_start
        logger.info(f"[{job_id}] [STAGE 2/4] Whisper transcription finished in {t_transcribe:.2f}s")

        transcript_file = get_transcript_file(job_id)
        with open(transcript_file, "w", encoding="utf-8") as f:
            json.dump(transcript_data, f, indent=2)

        # Step 5: Highlight selection (with timing)
        update_job_status(job_id, "suggesting", "Finding the best clip moments...", 75)
        t_highlights_start = time.perf_counter()
        candidates = pick_highlights(transcript_data, audio_wav=audio_wav)

        # Start each clip on the first spoken word and end it on the last one,
        # with 0.2s lead-in and 0.3s tail so no word gets cut off.
        segments = transcript_data.get("segments", [])
        for cand in candidates:
            cand["start"], cand["end"] = snap_to_word_boundaries(cand["start"], cand["end"], segments)
            cand["duration"] = round(cand["end"] - cand["start"], 2)

        t_highlights = time.perf_counter() - t_highlights_start
        logger.info(f"[{job_id}] [STAGE 3/4] Highlight selection finished in {t_highlights:.2f}s ({len(candidates)} clips chosen)")

        # Step 6: Render each clip sequentially (with timing)
        job_data = load_job(job_id)
        crop_style = job_data.get("crop_style", DEFAULT_CROP_STYLE) if job_data else DEFAULT_CROP_STYLE
        font_style_key = job_data.get("font_style", DEFAULT_FONT_STYLE) if job_data else DEFAULT_FONT_STYLE
        font_style = get_font_style(font_style_key)
        video_options = _video_render_options(job_data) if job_data else _video_render_options({})

        t_render_start = time.perf_counter()
        total_cands = len(candidates)
        failed_count = 0

        # Give every clip an id and "queued" status now, so the UI can show a card
        # (with a rendering placeholder) for all of them before work starts.
        for idx, cand in enumerate(candidates, start=1):
            cand["id"] = f"clip_{idx:02d}"
            cand["index"] = idx
            cand["render_status"] = "queued"
        _save_candidates_progress(job_id, candidates)

        for idx, cand in enumerate(candidates, start=1):
            clip_id = cand["id"]

            # Clamp the clip to the real video length so FFmpeg never runs past the end
            if cand["end"] > video_duration_sec:
                cand["end"] = round(video_duration_sec, 2)
                cand["duration"] = round(cand["end"] - cand["start"], 2)
            if cand["end"] <= cand["start"]:
                cand["render_status"] = "failed"
                cand["render_error"] = "clip starts after the end of the video"
                failed_count += 1
                logger.error(f"[{job_id}] Clip {idx}/{total_cands} skipped: {cand['render_error']}")
                _save_candidates_progress(job_id, candidates)
                continue

            pct = int(80 + (idx / max(1, total_cands)) * 18)
            msg = f"Rendering clip {idx} of {total_cands} ({crop_style})..."
            update_job_status(job_id, "rendering", msg, pct)
            cand["render_status"] = "rendering"
            _save_candidates_progress(job_id, candidates)

            t_clip_start = time.perf_counter()

            try:
                # Subtitles
                ass_path = job_dir / f"{clip_id}.ass"
                # Subtitles
                ass_path = job_dir / f"{clip_id}.ass"
                hook_title, hook_duration = _hook_for_candidate(cand, job_data) if job_data else (None, 3)
                generate_ass_subtitles_for_clip(
                    transcript_segments=segments,
                    clip_start=cand["start"],
                    clip_end=cand["end"],
                    output_ass_path=ass_path,
                    font_style=_caption_style_for(font_style, job_data or {}),
                    hook_title=hook_title,
                    hook_duration=hook_duration,
                    progress_bar=bool(job_data.get("progress_bar", False)) if job_data else False,
                    progress_color=job_data.get("progress_color", "#38BDF8") if job_data else "#38BDF8",
                )

                # MP4 render
                output_mp4 = job_dir / f"{clip_id}.mp4"
                render_vertical_clip(
                    input_video_path=input_video,
                    start_time=cand["start"],
                    end_time=cand["end"],
                    output_mp4_path=output_mp4,
                    subtitle_ass_path=ass_path,
                    crop_style=crop_style,
                    font_filename=font_style.get("filename"),
                    **video_options,
                )
            except Exception as e:
                # Mark just this clip as failed and carry on with the next one.
                # Full FFmpeg output is in logs/clip_XX.log; the UI gets a short reason.
                reason = (str(e).strip().splitlines()[0][:200] if str(e).strip() else "render error")
                cand["render_status"] = "failed"
                cand["render_error"] = reason
                failed_count += 1
                logger.error(f"[{job_id}] Clip {idx}/{total_cands} failed: {reason}")
                _save_candidates_progress(job_id, candidates)
                continue

            t_clip = time.perf_counter() - t_clip_start

            cand["rendered_file"] = f"{clip_id}.mp4"
            cand["rendered_url"] = f"/api/jobs/{job_id}/clips/{clip_id}.mp4"
            cand["render_status"] = "completed"
            cand["last_render_seconds"] = round(t_clip, 2)
            logger.info(f"[{job_id}] Rendered clip {idx}/{total_cands} ({cand['duration']}s) in {t_clip:.2f}s")

            # Compare the real file duration with what we asked for (ffprobe)
            _check_clip_duration(output_mp4, cand["duration"])

            # Persist this clip so the results page can show it right away
            _save_candidates_progress(job_id, candidates)

        succeeded_count = total_cands - failed_count

        t_render_total = time.perf_counter() - t_render_start
        logger.info(f"[{job_id}] [STAGE 4/4] Video clip rendering finished in {t_render_total:.2f}s")

        # Total Pipeline Time & Metrics Calculation
        t_total_pipeline = time.perf_counter() - t_pipeline_start
        sec_per_min = round(t_total_pipeline / (video_duration_sec / 60.0), 2) if video_duration_sec > 0 else 0.0

        timing_summary = {
            "video_duration_seconds": round(video_duration_sec, 2),
            "video_duration_minutes": video_duration_min,
            "audio_extraction_seconds": round(t_audio, 2),
            "transcription_seconds": round(t_transcribe, 2),
            "highlight_picking_seconds": round(t_highlights, 2),
            "rendering_seconds": round(t_render_total, 2),
            "total_processing_seconds": round(t_total_pipeline, 2),
            "seconds_per_minute_video": sec_per_min
        }

        # Terminal & Log Summary Banner
        summary_banner = f"""
========================================================================
[JOB TIMING BENCHMARK: {job_id}]
Video Length:             {video_duration_sec:.1f}s ({video_duration_min} mins)
------------------------------------------------------------------------
1. Audio Extraction:      {t_audio:.2f}s
2. Whisper Transcription: {t_transcribe:.2f}s
3. Highlight Selection:   {t_highlights:.2f}s
4. Video Clip Rendering:  {t_render_total:.2f}s  ({total_cands} clips)
------------------------------------------------------------------------
Total Processing Time:    {t_total_pipeline:.2f}s
Processing Speed Ratio:   {sec_per_min:.1f} seconds of processing per minute of video
========================================================================
"""
        logger.info(summary_banner)
        print(summary_banner)

        # Step 7: Save candidates and mark job completed with timing data
        candidates_file = get_candidates_file(job_id)
        with open(candidates_file, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2)

        job_data = load_job(job_id)
        if job_data:
            job_data["status"] = "completed"
            job_data["progress_message"] = (
                f"All {total_cands} clips rendered successfully in {t_total_pipeline:.1f}s! ({sec_per_min:.1f}s/min of video)"
                if failed_count == 0 else
                f"{succeeded_count} of {total_cands} clips rendered, {failed_count} failed. See logs/clip_XX.log for details."
            )
            job_data["progress_percent"] = 100
            job_data["duration_seconds"] = round(video_duration_sec, 2)
            job_data["language"] = transcript_data.get("language", "en")
            job_data["total_segments"] = transcript_data.get("total_segments", 0)
            job_data["total_words"] = transcript_data.get("total_words", 0)
            job_data["candidates"] = candidates
            job_data["timing"] = timing_summary
            save_job(job_id, job_data)

    except NoSpeechFoundError as e:
        logger.warning(f"[{job_id}] NoSpeechFoundError: {e}")
        update_job_status(job_id, "error", "No speech detected", 0, error=str(e))
    except ModelDownloadError as e:
        logger.error(f"[{job_id}] ModelDownloadError: {e}")
        update_job_status(job_id, "error", "Model download failed", 0, error=str(e))
    except Exception as e:
        full_tb = traceback.format_exc()
        logger.error(f"[{job_id}] Unhandled error processing job:\n{full_tb}")
        update_job_status(job_id, "error", "Processing error", 0, error=f"An unexpected error occurred: {str(e)}")

def _queue_worker_loop():
    """Continuous worker loop processing jobs one at a time."""
    while True:
        job_id = _job_queue.get()
        try:
            process_single_job(job_id)
        except Exception as e:
            logger.error(f"Error in queue worker for job {job_id}: {e}\n{traceback.format_exc()}")
        finally:
            _job_queue.task_done()

def enqueue_job(job_id: str) -> None:
    """Enqueues a job for background execution."""
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_queue_worker_loop, daemon=True)
        _worker_thread.start()
        logger.info("Background job queue worker thread started.")

    _job_queue.put(job_id)
    logger.info(f"Job {job_id} added to processing queue.")

def create_all_clips_zip(job_id: str) -> Path:
    """
    Creates a zip archive containing all rendered clip MP4 files for the job.
    """
    job_dir = get_job_dir(job_id)
    zip_path = job_dir / f"trimly_{job_id}_clips.zip"

    mp4_files = sorted(job_dir.glob("clip_*.mp4"))
    if not mp4_files:
        raise FileNotFoundError("No rendered clip files found to zip.")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for clip in mp4_files:
            zf.write(clip, arcname=clip.name)

    logger.info(f"Created ZIP archive with {len(mp4_files)} clips at {zip_path.name}")
    return zip_path
