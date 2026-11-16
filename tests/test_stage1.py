"""
Tests for Stage 1: Upload, job folders, limits, and legal disclaimer.
"""
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.config import JOBS_DIR, BASE_DIR

client = TestClient(app)

def test_home_page_contains_disclaimer():
    """Verify home page loads and contains the required creator disclaimer."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Only upload content you own or have permission to clip." in response.text

def test_reject_unsupported_file_extension():
    """Verify non-video files are rejected with a clear 400 error."""
    files = {"video_file": ("test.txt", b"This is not a video", "text/plain")}
    response = client.post("/api/upload", files=files)
    assert response.status_code == 400
    assert "Unsupported format '.txt'" in response.json()["detail"]

def test_upload_sample_video():
    """Verify uploading our sample MP4 video creates a valid job and stores the file."""
    sample_video_path = BASE_DIR / "sample_video.mp4"
    assert sample_video_path.exists(), "Sample video was not found."

    with open(sample_video_path, "rb") as f:
        files = {"video_file": ("sample_video.mp4", f, "video/mp4")}
        response = client.post("/api/upload", files=files)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    job_id = data["job_id"]
    assert job_id.startswith("job_")

    # Check job directory exists and file is saved
    job_folder = JOBS_DIR / job_id
    assert job_folder.exists()
    assert (job_folder / "input.mp4").exists()
    assert (job_folder / "job.json").exists()

    # Check job status endpoint
    status_resp = client.get(f"/api/jobs/{job_id}")
    assert status_resp.status_code == 200
    job_info = status_resp.json()
    assert job_info["status"] == "uploaded"
    assert job_info["original_filename"] == "sample_video.mp4"
