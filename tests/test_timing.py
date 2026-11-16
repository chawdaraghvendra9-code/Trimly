"""
Tests for timing metrics and stage-by-stage benchmarks.
"""
from pathlib import Path
from unittest.mock import patch
from app.config import BASE_DIR, JOBS_DIR
from app.jobs import create_job, process_single_job, load_job

def test_timing_metrics_in_job():
    """Verify that every stage is timed and benchmark metrics are calculated."""
    sample_video = BASE_DIR / "sample_video.mp4"
    assert sample_video.exists()

    job_info = create_job("sample_video.mp4", sample_video.stat().st_size)
    job_id = job_info["job_id"]
    job_dir = JOBS_DIR / job_id

    import shutil
    shutil.copyfile(sample_video, job_dir / "input.mp4")

    # Run processing with offline fallback for fast execution
    with patch("app.highlights.LLM_API_KEY", ""):
        process_single_job(job_id)

    updated = load_job(job_id)
    assert updated["status"] == "completed"
    assert "timing" in updated

    timing = updated["timing"]
    # Check all required fields are present
    assert "video_duration_seconds" in timing
    assert "video_duration_minutes" in timing
    assert "audio_extraction_seconds" in timing
    assert "transcription_seconds" in timing
    assert "highlight_picking_seconds" in timing
    assert "rendering_seconds" in timing
    assert "total_processing_seconds" in timing
    assert "seconds_per_minute_video" in timing

    # Check non-negative numerical values
    assert timing["video_duration_seconds"] > 0
    assert timing["audio_extraction_seconds"] >= 0
    assert timing["transcription_seconds"] >= 0
    assert timing["highlight_picking_seconds"] >= 0
    assert timing["rendering_seconds"] >= 0
    assert timing["total_processing_seconds"] >= 0
    assert timing["seconds_per_minute_video"] >= 0

    # Check ratio calculation
    expected_ratio = round(
        timing["total_processing_seconds"] / (timing["video_duration_seconds"] / 60.0), 2
    )
    assert abs(timing["seconds_per_minute_video"] - expected_ratio) < 0.05
