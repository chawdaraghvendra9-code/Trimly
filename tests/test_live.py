"""
Tests for live stream clipping and the one-time welcome notice.

Nothing here touches the real internet: ffmpeg and the stream capture are
replaced with fakes, and the link checks use localhost or a fake validator.
"""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app import live as live_module
from app import main as app_module
from app.live import (
    LiveStreamError,
    build_capture_command,
    capture_live_stream,
    resolve_live_source,
    validate_live_url,
)
from app.main import app

client = TestClient(app)


def _cleanup_job(job_id: str) -> None:
    shutil.rmtree(jobs.get_job_dir(job_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# Link checks
# ---------------------------------------------------------------------------

def test_live_link_must_be_http():
    with pytest.raises(LiveStreamError, match="http"):
        validate_live_url("ftp://example.com/live.m3u8")


def test_live_link_blocks_local_ports():
    with pytest.raises(LiveStreamError, match="port"):
        validate_live_url("http://127.0.0.1:8000/live.m3u8")


def test_live_link_blocks_private_networks():
    with pytest.raises(LiveStreamError):
        validate_live_url("http://localhost/live.m3u8")


def test_live_pages_need_youtube_import_enabled(monkeypatch):
    monkeypatch.setattr(live_module, "ENABLE_YOUTUBE_IMPORT", False)
    with pytest.raises(LiveStreamError, match="turned off"):
        validate_live_url("https://www.youtube.com/watch?v=abc123")


# ---------------------------------------------------------------------------
# ffmpeg command
# ---------------------------------------------------------------------------

def test_capture_command_is_a_safe_list(tmp_path):
    url = "https://example.com/live.m3u8?token=a&b=c"
    output = tmp_path / "input.mp4"

    command = build_capture_command("ffmpeg", url, 45, output)

    assert isinstance(command, list)
    assert command[0] == "ffmpeg"
    assert "-nostdin" in command
    assert "-y" in command
    assert command[command.index("-t") + 1] == "45"
    # The whole URL stays one argument, ampersand included.
    assert command[command.index("-i") + 1] == url
    assert "copy" in command
    assert str(output) == command[-1]


def test_capture_command_can_encode(tmp_path):
    command = build_capture_command(
        "ffmpeg", "https://example.com/live", 30, tmp_path / "input.mp4", encode=True
    )
    assert "libx264" in command
    assert "aac" in command
    assert "copy" not in command


def test_capture_retries_with_encoding(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(live_module, "find_ffmpeg_executable", lambda: "ffmpeg")

    def fake_capture(ffmpeg, url, seconds, output, log_path, encode, progress_cb):
        calls.append(encode)
        # The stream copy fails, the H.264/AAC retry succeeds.
        return encode

    monkeypatch.setattr(live_module, "_capture_once", fake_capture)

    capture_live_stream(
        "https://example.com/live.m3u8",
        tmp_path / "input.mp4",
        30,
        tmp_path / "live.log",
    )

    assert calls == [False, True]


def test_direct_live_link_needs_no_lookup(tmp_path):
    url = "https://example.com/live.m3u8"
    assert resolve_live_source(url, tmp_path) == url


# ---------------------------------------------------------------------------
# Job wiring
# ---------------------------------------------------------------------------

def test_live_job_starts_as_recording():
    job = jobs.create_job(
        "Live stream recording",
        0,
        source_url="https://example.com/live.m3u8",
        source="live",
    )
    try:
        assert job["source"] == "live"
        assert job["status"] == "recording"
        assert job["progress_percent"] == 0
    finally:
        _cleanup_job(job["job_id"])


def test_live_capture_queues_the_normal_pipeline(monkeypatch):
    job = jobs.create_job(
        "Live stream recording",
        0,
        source_url="https://example.com/live.m3u8",
        source="live",
    )
    job_id = job["job_id"]
    try:
        def fake_capture(url, output, seconds, log_path, progress_cb=None):
            output.write_bytes(b"not a real video, ffprobe is faked")
            if progress_cb:
                progress_cb(50)
            return output

        enqueued = []
        monkeypatch.setattr(jobs, "capture_live_stream", fake_capture)
        monkeypatch.setattr(
            jobs,
            "probe_video_file",
            lambda path: {"duration": 30.0, "width": 1280, "height": 720, "codec": "h264"},
        )
        monkeypatch.setattr(jobs, "enqueue_job", lambda queued_id: enqueued.append(queued_id))

        jobs._run_live_capture(job_id, "https://example.com/live.m3u8", 30)

        data = jobs.load_job(job_id)
        assert data["status"] == "uploaded"
        assert data["source"] == "live"
        assert data["duration_seconds"] == 30.0
        assert data["video_width"] == 1280
        assert data["capture_seconds"] == 30
        assert enqueued == [job_id]
    finally:
        _cleanup_job(job_id)


def test_live_capture_failure_is_shown_in_the_job(monkeypatch):
    job = jobs.create_job(
        "Live stream recording",
        0,
        source_url="https://example.com/live.m3u8",
        source="live",
    )
    job_id = job["job_id"]
    try:
        def failing_capture(url, output, seconds, log_path, progress_cb=None):
            output.write_bytes(b"partial recording")
            raise LiveStreamError("The stream ended before recording finished.")

        monkeypatch.setattr(jobs, "capture_live_stream", failing_capture)

        jobs._run_live_capture(job_id, "https://example.com/live.m3u8", 30)

        data = jobs.load_job(job_id)
        assert data["status"] == "error"
        assert data["error"] == "The stream ended before recording finished."
        # The partial file is removed so the pipeline never picks it up.
        assert not (jobs.get_job_dir(job_id) / "input.mp4").exists()
    finally:
        _cleanup_job(job_id)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

def test_api_live_creates_a_job(monkeypatch):
    started = []
    monkeypatch.setattr(app_module, "validate_live_url", lambda url: url.strip())
    monkeypatch.setattr(
        app_module,
        "start_live_capture",
        lambda job_id, url, seconds: started.append((job_id, url, seconds)),
    )

    response = client.post(
        "/api/live",
        json={"url": "https://example.com/live.m3u8", "seconds": 5},
    )
    assert response.status_code == 200
    data = response.json()
    job_id = data["job_id"]
    try:
        assert data["status"] == "recording"
        # Too-short requests are raised to the minimum, never rejected.
        assert data["capture_seconds"] == 20
        assert started == [(job_id, "https://example.com/live.m3u8", 20)]

        saved = jobs.load_job(job_id)
        assert saved["source"] == "live"
        assert saved["status"] == "recording"
    finally:
        _cleanup_job(job_id)


def test_api_live_rejects_a_bad_link(monkeypatch):
    def reject(url):
        raise LiveStreamError("That is not a live stream link.")

    monkeypatch.setattr(app_module, "validate_live_url", reject)

    response = client.post("/api/live", json={"url": "not-a-link"})
    assert response.status_code == 400
    assert response.json()["detail"] == "That is not a live stream link."


def test_welcome_notice_is_shown_only_once(tmp_path, monkeypatch):
    marker = tmp_path / "welcome_seen"
    monkeypatch.setattr(app_module, "WELCOME_MARKER", marker)

    first = client.get("/")
    assert first.status_code == 200
    assert "runs on your computer" in first.text

    seen = client.post("/api/welcome/seen")
    assert seen.status_code == 200
    assert seen.json()["success"] is True
    assert marker.exists()

    second = client.get("/")
    assert second.status_code == 200
    assert "runs on your computer" not in second.text
