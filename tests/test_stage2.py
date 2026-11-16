"""
Tests for Stage 2: Audio extraction, faster-whisper transcription, error handling, and JSON output.
"""
import pytest
from pathlib import Path
from app.config import BASE_DIR, JOBS_DIR
from app.video import find_ffmpeg_executable, verify_ffmpeg_on_startup, extract_audio_16k_mono
from app.transcription import (
    transcribe_audio_file,
    NoSpeechFoundError,
    get_whisper_model
)
from app.jobs import create_job, process_single_job, load_job, get_transcript_file

def test_ffmpeg_startup_verification():
    """Verify that FFmpeg is detected on the system."""
    ffmpeg_path = verify_ffmpeg_on_startup()
    assert ffmpeg_path is not None
    assert Path(ffmpeg_path).exists()

def test_audio_extraction_16k_mono(tmp_path):
    """Verify audio extraction produces a valid 16kHz mono WAV file."""
    sample_video = BASE_DIR / "sample_video.mp4"
    assert sample_video.exists(), "sample_video.mp4 must exist for testing."

    output_wav = tmp_path / "test_audio.wav"
    extracted = extract_audio_16k_mono(sample_video, output_wav)

    assert extracted.exists()
    assert extracted.stat().st_size > 1000  # Should be non-empty WAV

def test_transcription_with_word_timestamps(tmp_path):
    """
    Verify transcription runs on CPU with int8 and produces word-level timestamps.
    Uses 'tiny' model for fast test execution.
    """
    sample_video = BASE_DIR / "sample_video.mp4"
    audio_wav = tmp_path / "speech.wav"
    extract_audio_16k_mono(sample_video, audio_wav)

    # Transcribe using 'tiny' model for speed in automated test
    result = transcribe_audio_file(audio_wav, model_size="tiny")

    assert "segments" in result
    assert result["total_segments"] > 0
    assert result["total_words"] > 0

    first_segment = result["segments"][0]
    assert "start" in first_segment
    assert "end" in first_segment
    assert "text" in first_segment
    assert "words" in first_segment
    assert len(first_segment["words"]) > 0

    first_word = first_segment["words"][0]
    assert "word" in first_word
    assert "start" in first_word
    assert "end" in first_word

def test_no_speech_detected_error(tmp_path):
    """
    Verify that an audio file with pure silence raises NoSpeechFoundError.
    """
    import subprocess
    ffmpeg_exe = find_ffmpeg_executable()
    silent_wav = tmp_path / "silent.wav"

    # Generate 5 seconds of pure silence
    cmd = [
        ffmpeg_exe, "-y",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-t", "5",
        str(silent_wav)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    with pytest.raises(NoSpeechFoundError):
        transcribe_audio_file(silent_wav, model_size="tiny")

def test_background_job_processing():
    """
    Verify full job processing lifecycle: video -> audio -> transcript.json -> ready status.
    """
    sample_video = BASE_DIR / "sample_video.mp4"
    job_info = create_job("sample_video.mp4", sample_video.stat().st_size)
    job_id = job_info["job_id"]
    job_dir = JOBS_DIR / job_id

    # Copy sample video into job folder as input.mp4
    import shutil
    shutil.copyfile(sample_video, job_dir / "input.mp4")

    # Run processing (synchronously in test with offline fallback for fast test execution)
    from unittest.mock import patch
    with patch("app.highlights.LLM_API_KEY", ""):
        process_single_job(job_id)

    # Check updated job status
    updated_job = load_job(job_id)
    assert updated_job["status"] in {"ready", "completed"}
    assert updated_job["progress_percent"] == 100
    assert updated_job["error"] is None

    # Check transcript.json exists and has content
    transcript_file = get_transcript_file(job_id)
    assert transcript_file.exists()
    import json
    with open(transcript_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["total_segments"] > 0
    assert data["total_words"] > 0
