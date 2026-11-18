"""
Cliptor Clip Studio - Main FastAPI Web Application
Handles web pages, video uploads, background job queues, clip streaming, trimming, and downloads.
"""
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import unquote, urlparse
import re
import tempfile

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
from app.downloader import (
    LinkError,
    check_ytdlp_runtime,
    convert_share_link,
    validate_public_url,
)
from app.jobs import (
    create_job,
    load_job,
    save_job,
    get_job_dir,
    get_transcript_file,
    get_candidates_file,
    enqueue_job,
    render_single_candidate,
    create_all_clips_zip,
    start_link_import
)
from app.fonts import DEFAULT_FONT_STYLE, FONT_STYLES, list_font_styles
from app.backgrounds import (
    BACKGROUND_EXTENSIONS,
    check_background,
    list_backgrounds,
    safe_background_name,
    store_background,
)
from app.video import verify_ffmpeg_on_startup

class TrimClipRequest(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    crop_style: Optional[str] = None
    font_style: Optional[str] = None
    hook_title: Optional[str] = None

class FontStyleRequest(BaseModel):
    font_style: str

class VideoStyleRequest(BaseModel):
    video_style: Optional[str] = None
    solid_color: Optional[str] = None
    gradient_top: Optional[str] = None
    gradient_bottom: Optional[str] = None
    progress_bar: Optional[bool] = None
    progress_color: Optional[str] = None
    hook_enabled: Optional[bool] = None
    hook_duration: Optional[int] = None
    smart_aspect: Optional[str] = None
    smart_zoom: Optional[bool] = None
    smart_background: Optional[str] = None
    split_background: Optional[str] = None
    split_divider: Optional[bool] = None

class ImportLinkRequest(BaseModel):
    url: str
    crop_style: Optional[str] = None

VIDEO_STYLES = {"smart_fit", "split", "blurred", "solid", "gradient"}
SMART_ASPECTS = {"4:5", "1:1", "3:4"}
SMART_BACKGROUNDS = {"blurred", "solid", "gradient"}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_BACKGROUND_UPLOAD_BYTES = 100 * 1024 * 1024

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
        runtime_problem = check_ytdlp_runtime()
        if runtime_problem:
            logger.error(runtime_problem)
            print(runtime_problem)
        else:
            logger.info("Link import runtime ready (Node.js 22+ and yt_dlp_ejs).")
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

# Caption fonts are served so the results page can show each name in its own font.
app.mount("/fonts", StaticFiles(directory=str(BASE_DIR / "fonts")), name="fonts")

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


@app.post("/api/import-link")
def import_from_link(req: ImportLinkRequest):
    """
    Starts a background download of a direct video link, or of a public
    Google Drive / Dropbox share link.

    The link is checked before the job is created, so bad links fail instantly
    with a friendly message instead of leaving a broken job behind.
    """
    raw_url = (req.url or "").strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="Please paste a link first.")

    chosen_style = (req.crop_style or DEFAULT_CROP_STYLE).strip().lower()
    if chosen_style not in {"blurred_background", "center_crop"}:
        chosen_style = DEFAULT_CROP_STYLE

    # Convert share links, then refuse anything that is not a public address.
    # (This runs again for every redirect during the actual download.)
    try:
        direct_url, provider = convert_share_link(raw_url)
        validate_public_url(direct_url)
    except LinkError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # yt-dlp needs Node.js 22+ and yt_dlp_ejs for streaming sites.
    if provider in {"youtube", "tiktok", "instagram"}:
        runtime_problem = check_ytdlp_runtime()
        if runtime_problem:
            raise HTTPException(status_code=400, detail=runtime_problem)

    # Only used as a readable name in job.json, never as a file path.
    friendly_names = {
        "youtube": "YouTube video",
        "tiktok": "TikTok video",
        "instagram": "Instagram video",
    }
    link_name = friendly_names.get(provider) or (
        Path(unquote(urlparse(raw_url).path)).name or "video-link"
    )

    job_info = create_job(
        original_filename=link_name,
        file_size_bytes=0,
        crop_style=chosen_style,
        source_url=raw_url,
    )
    job_id = job_info["job_id"]
    logger.info(f"[{job_id}] Import from link requested ({provider}).")

    start_link_import(job_id, raw_url)

    return JSONResponse({
        "success": True,
        "job_id": job_id,
        "status": "downloading",
        "provider": provider,
        "crop_style": chosen_style,
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
            new_crop_style=req.crop_style,
            new_font_style=req.font_style,
            new_hook_title=req.hook_title
        )
        return JSONResponse({"success": True, "candidate": updated_cand})
    except Exception as e:
        logger.exception(f"Error re-rendering clip {clip_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/jobs/{job_id}/font-style")
async def set_job_font_style(job_id: str, req: FontStyleRequest):
    """
    Saves the caption font for the whole project. Existing clips keep their old
    look until they are re-rendered; every new render uses this choice.
    """
    if req.font_style not in FONT_STYLES:
        raise HTTPException(status_code=400, detail="Unknown caption font.")

    job_data = load_job(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found.")

    job_data["font_style"] = req.font_style
    save_job(job_id, job_data)
    style = FONT_STYLES[req.font_style]
    return JSONResponse({"success": True, "font_style": req.font_style, "label": style["label"]})


@app.post("/api/jobs/{job_id}/video-style")
async def set_job_video_style(job_id: str, req: VideoStyleRequest):
    """
    Saves the project-wide video look, hook and progress bar. Existing clips
    keep their old look until they are re-rendered.
    """
    job_data = load_job(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found.")

    if req.video_style is not None:
        if req.video_style not in VIDEO_STYLES:
            raise HTTPException(status_code=400, detail="Unknown video style.")
        job_data["video_style"] = req.video_style

    for field in ("solid_color", "gradient_top", "gradient_bottom", "progress_color"):
        value = getattr(req, field)
        if value is not None:
            if not HEX_COLOR_RE.match(value):
                raise HTTPException(status_code=400, detail=f"{field} must be a #RRGGBB color.")
            job_data[field] = value

    if req.progress_bar is not None:
        job_data["progress_bar"] = bool(req.progress_bar)

    if req.hook_enabled is not None:
        job_data["hook_enabled"] = bool(req.hook_enabled)

    if req.hook_duration is not None:
        if not 1 <= int(req.hook_duration) <= 6:
            raise HTTPException(status_code=400, detail="Hook duration must be between 1 and 6 seconds.")
        job_data["hook_duration"] = int(req.hook_duration)

    if req.smart_aspect is not None:
        if req.smart_aspect not in SMART_ASPECTS:
            raise HTTPException(status_code=400, detail="Smart fit aspect must be 4:5, 1:1 or 3:4.")
        job_data["smart_aspect"] = req.smart_aspect

    if req.smart_zoom is not None:
        job_data["smart_zoom"] = bool(req.smart_zoom)

    if req.smart_background is not None:
        if req.smart_background not in SMART_BACKGROUNDS:
            raise HTTPException(status_code=400, detail="Unknown background for smart fit.")
        job_data["smart_background"] = req.smart_background

    if req.split_background is not None:
        # 'random', a file name from backgrounds/, or 'none' for the dark fallback.
        name = str(req.split_background).strip()
        if name and name not in ("random", "none"):
            known = {item["name"] for item in list_backgrounds()}
            if name not in known:
                raise HTTPException(status_code=400, detail="That background clip was not found.")
        job_data["split_background"] = name or "none"

    if req.split_divider is not None:
        job_data["split_divider"] = bool(req.split_divider)

    save_job(job_id, job_data)
    return JSONResponse({
        "success": True,
        "video_style": job_data.get("video_style", "smart_fit"),
        "solid_color": job_data.get("solid_color", "#000000"),
        "gradient_top": job_data.get("gradient_top", "#0F172A"),
        "gradient_bottom": job_data.get("gradient_bottom", "#1E3A8A"),
        "progress_bar": bool(job_data.get("progress_bar", False)),
        "progress_color": job_data.get("progress_color", "#38BDF8"),
        "hook_enabled": bool(job_data.get("hook_enabled", False)),
        "hook_duration": int(job_data.get("hook_duration", 3)),
        "smart_aspect": job_data.get("smart_aspect", "4:5"),
        "smart_zoom": bool(job_data.get("smart_zoom", False)),
        "smart_background": job_data.get("smart_background", "blurred"),
        "split_background": job_data.get("split_background", "random"),
        "split_divider": bool(job_data.get("split_divider", True)),
    })


@app.get("/api/backgrounds")
async def get_backgrounds():
    """The background clips available to the Split screen style."""
    return JSONResponse({"backgrounds": list_backgrounds()})


@app.post("/api/backgrounds/upload")
async def upload_background(file: UploadFile = File(...)):
    """
    Saves the user's own background clip into backgrounds/. It must be
    .mp4/.webm/.mov, at most 60 seconds and 100 MB, and pass ffprobe first.
    """
    filename = file.filename or "background.mp4"
    if Path(filename).suffix.lower() not in BACKGROUND_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Use an .mp4, .webm or .mov background clip.")

    with tempfile.NamedTemporaryFile(
        delete=False, suffix=Path(filename).suffix.lower(), prefix="cliptor_bg_"
    ) as temporary:
        temporary_path = Path(temporary.name)

    try:
        total = 0
        with open(temporary_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BACKGROUND_UPLOAD_BYTES:
                    raise HTTPException(status_code=400, detail="That background is larger than 100 MB.")
                out.write(chunk)

        if total == 0:
            raise HTTPException(status_code=400, detail="That file is empty.")

        problem = check_background(temporary_path)
        if problem:
            raise HTTPException(status_code=400, detail=problem)

        stored = store_background(filename, temporary_path)
    except HTTPException:
        temporary_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        temporary_path.unlink(missing_ok=True)
        logger.exception(f"Background upload failed: {e}")
        raise HTTPException(status_code=500, detail="The background clip could not be saved.")
    finally:
        await file.close()

    return JSONResponse({
        "success": True,
        "name": stored.name,
        "backgrounds": list_backgrounds(),
    })


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
        context={
            "job": job_data,
            "font_styles": list_font_styles(),
            "current_font_style": job_data.get("font_style", DEFAULT_FONT_STYLE),
            "default_font_style": DEFAULT_FONT_STYLE,
            "video_settings": {
                "video_style": job_data.get("video_style", "smart_fit"),
                "solid_color": job_data.get("solid_color", "#000000"),
                "gradient_top": job_data.get("gradient_top", "#0F172A"),
                "gradient_bottom": job_data.get("gradient_bottom", "#1E3A8A"),
                "progress_bar": bool(job_data.get("progress_bar", False)),
                "progress_color": job_data.get("progress_color", "#38BDF8"),
                "hook_enabled": bool(job_data.get("hook_enabled", False)),
                "hook_duration": int(job_data.get("hook_duration", 3)),
                "smart_aspect": job_data.get("smart_aspect", "4:5"),
                "smart_zoom": bool(job_data.get("smart_zoom", False)),
                "smart_background": job_data.get("smart_background", "blurred"),
                "split_background": job_data.get("split_background", "random"),
                "split_divider": bool(job_data.get("split_divider", True)),
            },
            "backgrounds": list_backgrounds(),
        }
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
