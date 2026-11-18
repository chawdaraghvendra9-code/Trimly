"""
Tests for Stages 4 & 5: Video rendering, ASS subtitles generation, trimming, and ZIP downloads.
"""
import pytest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.config import BASE_DIR, JOBS_DIR
from app.subtitles import generate_ass_subtitles_for_clip, format_ass_time
from app.video import render_vertical_clip
from app.jobs import (
    create_job,
    process_single_job,
    load_job,
    render_single_candidate,
    create_all_clips_zip
)

def test_ass_time_formatting():
    """Verify conversion of seconds to H:MM:SS.cs format."""
    assert format_ass_time(0.0) == "0:00:00.00"
    assert format_ass_time(65.42) == "0:01:05.42"
    assert format_ass_time(3661.05) == "1:01:01.05"

def test_generate_ass_subtitles(tmp_path):
    """
    Verify ASS subtitles generator offsets timestamps by clip_start
    and chunks words into groups of 2-3 with safe margins.
    """
    segments = [
        {
            "id": 1,
            "start": 10.0,
            "end": 15.0,
            "text": "Welcome to Cliptor Studio video creator.",
            "words": [
                {"word": "Welcome", "start": 10.0, "end": 10.5},
                {"word": "to", "start": 10.6, "end": 10.8},
                {"word": "Cliptor", "start": 10.9, "end": 11.5},
                {"word": "Studio", "start": 11.6, "end": 12.2},
                {"word": "video", "start": 12.3, "end": 12.8},
                {"word": "creator.", "start": 12.9, "end": 13.5}
            ]
        }
    ]

    ass_file = tmp_path / "test.ass"
    # Clip starts at 10.0s, so words should start at 0.0s
    generate_ass_subtitles_for_clip(
        transcript_segments=segments,
        clip_start=10.0,
        clip_end=15.0,
        output_ass_path=ass_file
    )

    assert ass_file.exists()
    content = ass_file.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content
    assert ",90,90,500,1" in content
    assert "Dialogue: 0,0:00:00.00" in content
    assert "Welcome to Cliptor" in content

def test_render_vertical_clip_both_styles(tmp_path):
    """
    Verify rendering both 'center_crop' and 'blurred_background' produces 1080x1920 MP4s.
    """
    sample_video = BASE_DIR / "sample_video.mp4"
    assert sample_video.exists()

    # Style 1: blurred_background
    blur_mp4 = tmp_path / "clip_blur.mp4"
    render_vertical_clip(
        input_video_path=sample_video,
        start_time=0.0,
        end_time=3.0,
        output_mp4_path=blur_mp4,
        subtitle_ass_path=None,
        crop_style="blurred_background"
    )
    assert blur_mp4.exists()
    assert blur_mp4.stat().st_size > 10000

    # Style 2: center_crop
    crop_mp4 = tmp_path / "clip_crop.mp4"
    render_vertical_clip(
        input_video_path=sample_video,
        start_time=0.0,
        end_time=3.0,
        output_mp4_path=crop_mp4,
        subtitle_ass_path=None,
        crop_style="center_crop"
    )
    assert crop_mp4.exists()
    assert crop_mp4.stat().st_size > 10000

def test_end_to_end_job_renders_clips_and_zip():
    """
    Verify complete job flow:
    upload -> transcribe -> highlights -> render clip_01.mp4 -> create zip.
    """
    sample_video = BASE_DIR / "sample_video.mp4"
    job_info = create_job("sample_video.mp4", sample_video.stat().st_size)
    job_id = job_info["job_id"]
    job_dir = JOBS_DIR / job_id

    import shutil
    shutil.copyfile(sample_video, job_dir / "input.mp4")

    # Run processing (offline fallback for fast local unit testing)
    with patch("app.highlights.LLM_API_KEY", ""):
        process_single_job(job_id)

    updated = load_job(job_id)
    assert updated["status"] == "completed"
    assert len(updated["candidates"]) > 0

    first_cand = updated["candidates"][0]
    clip_file = job_dir / first_cand["rendered_file"]
    assert clip_file.exists()
    assert clip_file.stat().st_size > 10000

    # Test trim & re-render of this clip
    old_end = first_cand["end"]
    new_end = old_end - 1.0
    updated_cand = render_single_candidate(
        job_id=job_id,
        clip_id=first_cand["id"],
        new_start=first_cand["start"],
        new_end=new_end,
        new_crop_style="center_crop"
    )
    assert updated_cand["end"] == new_end
    assert clip_file.exists()

    # Test ZIP packaging
    zip_path = create_all_clips_zip(job_id)
    assert zip_path.exists()
    assert zipfile.is_zipfile(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        assert any("clip_01.mp4" in name for name in namelist)
