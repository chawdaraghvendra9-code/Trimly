"""
Job management and background execution for Cliptor Clip Studio.
Processes jobs one at a time: audio extraction -> transcription -> highlight suggestions -> rendering vertical clips.
Includes detailed stage-by-stage timing instrumentation and benchmark metrics.
"""
import json
import uuid
import time
import threading
import queue
import traceback
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional, List

from app.config import JOBS_DIR, WHISPER_MODEL, DEFAULT_CROP_STYLE
from app.logger import logger
from app.video import (
    extract_audio_16k_mono,
    find_ffmpeg_executable,
    FFMPEG_INSTALL_INSTRUCTIONS,
    render_vertical_clip,
    get_audio_duration_seconds
)
from app.transcription import (
    transcribe_audio_file,
    NoSpeechFoundError,
    ModelDownloadError,
    TranscriptionError
)
from app.highlights import pick_highlights
from app.subtitles import generate_ass_subtitles_for_clip

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
    """Saves updated job data to job.json."""
    job_file = get_job_file(job_id)
    try:
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
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

def create_job(original_filename: str, file_size_bytes: int, crop_style: str = DEFAULT_CROP_STYLE) -> Dict[str, Any]:
    """
    Creates a new job with a unique folder and an initial job.json file.
    """
    job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    job_dir = get_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    job_data: Dict[str, Any] = {
        "job_id": job_id,
        "original_filename": original_filename,
        "file_size_bytes": file_size_bytes,
        "crop_style": crop_style,
        "status": "uploaded",
        "progress_message": "Video uploaded. Queued for processing...",
        "progress_percent": 5,
        "duration_seconds": 0.0,
        "created_at": time.time(),
        "error": None,
        "candidates": [],
        "rendered_clips": [],
        "timing": {}
    }

    save_job(job_id, job_data)
    return job_data

def render_single_candidate(
    job_id: str,
    clip_id: str,
    new_start: Optional[float] = None,
    new_end: Optional[float] = None,
    new_crop_style: Optional[str] = None
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

    # Generate ASS subtitles for this clip
    ass_path = job_dir / f"{clip_id}.ass"
    generate_ass_subtitles_for_clip(
        transcript_segments=segments,
        clip_start=target_cand["start"],
        clip_end=target_cand["end"],
        output_ass_path=ass_path
    )

    # Render MP4
    output_mp4 = job_dir / f"{clip_id}.mp4"
    render_vertical_clip(
        input_video_path=input_video,
        start_time=target_cand["start"],
        end_time=target_cand["end"],
        output_mp4_path=output_mp4,
        subtitle_ass_path=ass_path,
        crop_style=crop_style
    )

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
        t_highlights = time.perf_counter() - t_highlights_start
        logger.info(f"[{job_id}] [STAGE 3/4] Highlight selection finished in {t_highlights:.2f}s ({len(candidates)} clips chosen)")

        # Step 6: Render each clip sequentially (with timing)
        job_data = load_job(job_id)
        crop_style = job_data.get("crop_style", DEFAULT_CROP_STYLE) if job_data else DEFAULT_CROP_STYLE
        segments = transcript_data.get("segments", [])

        t_render_start = time.perf_counter()
        total_cands = len(candidates)
        failed_count = 0
        for idx, cand in enumerate(candidates, start=1):
            clip_id = f"clip_{idx:02d}"
            cand["id"] = clip_id
            cand["index"] = idx

            # Clamp the clip to the real video length so FFmpeg never runs past the end
            if cand["end"] > video_duration_sec:
                cand["end"] = round(video_duration_sec, 2)
                cand["duration"] = round(cand["end"] - cand["start"], 2)
            if cand["end"] <= cand["start"]:
                cand["render_status"] = "failed"
                cand["render_error"] = "clip starts after the end of the video"
                failed_count += 1
                logger.error(f"[{job_id}] Clip {idx}/{total_cands} skipped: {cand['render_error']}")
                continue

            pct = int(80 + (idx / max(1, total_cands)) * 18)
            msg = f"Rendering clip {idx} of {total_cands} ({crop_style})..."
            update_job_status(job_id, "rendering", msg, pct)

            t_clip_start = time.perf_counter()

            try:
                # Subtitles
                ass_path = job_dir / f"{clip_id}.ass"
                generate_ass_subtitles_for_clip(
                    transcript_segments=segments,
                    clip_start=cand["start"],
                    clip_end=cand["end"],
                    output_ass_path=ass_path
                )

                # MP4 render
                output_mp4 = job_dir / f"{clip_id}.mp4"
                render_vertical_clip(
                    input_video_path=input_video,
                    start_time=cand["start"],
                    end_time=cand["end"],
                    output_mp4_path=output_mp4,
                    subtitle_ass_path=ass_path,
                    crop_style=crop_style
                )
            except Exception as e:
                # Mark just this clip as failed and carry on with the next one.
                # Full FFmpeg output is in logs/clip_XX.log; the UI gets a short reason.
                reason = (str(e).strip().splitlines()[0][:200] if str(e).strip() else "render error")
                cand["render_status"] = "failed"
                cand["render_error"] = reason
                failed_count += 1
                logger.error(f"[{job_id}] Clip {idx}/{total_cands} failed: {reason}")
                continue

            t_clip = time.perf_counter() - t_clip_start

            cand["rendered_file"] = f"{clip_id}.mp4"
            cand["rendered_url"] = f"/api/jobs/{job_id}/clips/{clip_id}.mp4"
            cand["render_status"] = "completed"
            cand["last_render_seconds"] = round(t_clip, 2)
            logger.info(f"[{job_id}] Rendered clip {idx}/{total_cands} ({cand['duration']}s) in {t_clip:.2f}s")

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
    zip_path = job_dir / f"cliptor_{job_id}_clips.zip"

    mp4_files = sorted(job_dir.glob("clip_*.mp4"))
    if not mp4_files:
        raise FileNotFoundError("No rendered clip files found to zip.")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for clip in mp4_files:
            zf.write(clip, arcname=clip.name)

    logger.info(f"Created ZIP archive with {len(mp4_files)} clips at {zip_path.name}")
    return zip_path
