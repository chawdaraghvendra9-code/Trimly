"""
Cliptor Clip Studio - Main FastAPI Web Application
Handles web pages, video uploads, background job queues, clip streaming, trimming, and downloads.
"""
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import (
    BASE_DIR,
    JOBS_DIR,
    MAX_FILE_SIZE_MB,
    MAX_FILE_SIZE_BYTES,
    MAX_VIDEO_MINUTES,
    ALLOWED_EXTENSIONS,
    WHISPER_MODEL,
    DEFAULT_CROP_STYLE
)
from app.logger import logger
from app.jobs import (
    create_job,
    load_job,
    get_job_dir,
    get_transcript_file,
    get_candidates_file,
    enqueue_job,
    render_single_candidate,
    create_all_clips_zip
)
from app.video import verify_ffmpeg_on_startup

class TrimClipRequest(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    crop_style: Optional[str] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs on server startup and shutdown.
    Verifies that FFmpeg is installed and accessible.
    """
    logger.info("Initializing Cliptor Clip Studio...")
    try:
        ffmpeg_path = verify_ffmpeg_on_startup()
        logger.info(f"FFmpeg is ready at: {ffmpeg_path}")
        logger.info(f"Whisper Model configured: '{WHISPER_MODEL}' (compute_type=int8, device=cpu)")
    except Exception as e:
        logger.error(f"FFmpeg startup verification failed: {e}")
    yield
    logger.info("Cliptor Clip Studio shutting down.")

app = FastAPI(
    title="Cliptor Clip Studio",
    description="Turn long videos into 9:16 vertical clips for Reels, Shorts, and TikTok",
    version="0.5.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Setup Jinja2 templates
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Renders the main upload page."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "max_file_size_mb": MAX_FILE_SIZE_MB,
            "max_video_minutes": MAX_VIDEO_MINUTES,
            "whisper_model": WHISPER_MODEL,
            "default_crop_style": DEFAULT_CROP_STYLE
        }
    )


@app.post("/api/upload")
async def upload_video(
    video_file: UploadFile = File(...),
    crop_style: str = Form(DEFAULT_CROP_STYLE)
):
    """
    Uploads video file, validates format and size, and queues full processing + rendering.
    """
    if not video_file.filename:
        raise HTTPException(status_code=400, detail="No file was selected.")

    file_ext = Path(video_file.filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        allowed_list = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{file_ext}'. Allowed formats: {allowed_list}"
        )

    # Validate crop style
    chosen_style = crop_style.strip().lower()
    if chosen_style not in {"blurred_background", "center_crop"}:
        chosen_style = DEFAULT_CROP_STYLE

    # Read chunks safely
    chunk_size = 1024 * 1024
    total_bytes = 0
    temp_chunks = []

    try:
        while True:
            chunk = await video_file.read(chunk_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_FILE_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds maximum allowed size of {MAX_FILE_SIZE_MB} MB."
                )
            temp_chunks.append(chunk)
    finally:
        await video_file.close()

    # Create job
    job_info = create_job(
        original_filename=video_file.filename,
        file_size_bytes=total_bytes,
        crop_style=chosen_style
    )
    job_id = job_info["job_id"]
    job_dir = get_job_dir(job_id)

    # Save video file
    input_video_path = job_dir / f"input{file_ext}"
    with open(input_video_path, "wb") as f:
        for chunk in temp_chunks:
            f.write(chunk)

    logger.info(f"Saved upload to {input_video_path} (Style: {chosen_style})")

    # Enqueue background pipeline
    enqueue_job(job_id)

    return JSONResponse({
        "success": True,
        "job_id": job_id,
        "filename": video_file.filename,
        "crop_style": chosen_style,
        "size_mb": round(total_bytes / (1024 * 1024), 2),
        "status": "uploaded"
    })


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Returns the current JSON status for the job."""
    job_data = load_job(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse(job_data)


@app.get("/api/jobs/{job_id}/transcript")
async def get_job_transcript(job_id: str):
    """Returns the full transcript JSON."""
    transcript_file = get_transcript_file(job_id)
    if not transcript_file.exists():
        raise HTTPException(status_code=404, detail="Transcript is not ready yet.")
    return FileResponse(path=str(transcript_file), media_type="application/json")


@app.get("/api/jobs/{job_id}/candidates")
async def get_job_candidates(job_id: str):
    """Returns the suggested clip candidates for this job."""
    candidates_file = get_candidates_file(job_id)
    if not candidates_file.exists():
        raise HTTPException(status_code=404, detail="Clip candidates are not ready yet.")
    return FileResponse(path=str(candidates_file), media_type="application/json")


@app.get("/api/jobs/{job_id}/clips/{clip_filename}")
async def stream_rendered_clip(job_id: str, clip_filename: str):
    """Streams a rendered vertical 9:16 clip MP4 file."""
    job_dir = get_job_dir(job_id)
    clip_path = job_dir / clip_filename
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail=f"Clip {clip_filename} not found.")
    return FileResponse(path=str(clip_path), media_type="video/mp4")


@app.get("/api/jobs/{job_id}/clips/{clip_filename}/download")
async def download_single_clip(job_id: str, clip_filename: str):
    """Forces browser download of a specific clip MP4 file."""
    job_dir = get_job_dir(job_id)
    clip_path = job_dir / clip_filename
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found.")
    return FileResponse(
        path=str(clip_path),
        media_type="video/mp4",
        filename=clip_filename
    )


@app.post("/api/jobs/{job_id}/clips/{clip_id}/render")
async def re_render_clip(job_id: str, clip_id: str, req: TrimClipRequest):
    """
    Trims/nudges start and end time, or switches crop style, and re-renders ONLY that clip.
    """
    try:
        updated_cand = render_single_candidate(
            job_id=job_id,
            clip_id=clip_id,
            new_start=req.start,
            new_end=req.end,
            new_crop_style=req.crop_style
        )
        return JSONResponse({"success": True, "candidate": updated_cand})
    except Exception as e:
        logger.exception(f"Error re-rendering clip {clip_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs/{job_id}/download-all")
async def download_all_clips(job_id: str):
    """Generates and serves a ZIP archive containing all rendered clips for the job."""
    try:
        zip_path = create_all_clips_zip(job_id)
        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=f"cliptor_{job_id}_all_clips.zip"
        )
    except Exception as e:
        logger.exception(f"Error generating ZIP archive: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create ZIP: {str(e)}")


@app.get("/jobs/{job_id}/view", response_class=HTMLResponse)
async def view_job(request: Request, job_id: str):
    """Renders the studio editor view."""
    job_data = load_job(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found.")

    return templates.TemplateResponse(
        request=request,
        name="editor.html",
        context={"job": job_data}
    )


@app.get("/api/jobs/{job_id}/video")
async def stream_job_video(job_id: str):
    """Streams original video file."""
    job_dir = get_job_dir(job_id)
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found.")
    matching = list(job_dir.glob("input.*"))
    if not matching:
        raise HTTPException(status_code=404, detail="Video file not found.")
    return FileResponse(path=str(matching[0]), media_type="video/mp4")
