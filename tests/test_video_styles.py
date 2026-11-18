"""
Video style tests.

Each style is rendered for 1.5 seconds from a generated clip (never a user
video) and checked with ffprobe: 1080x1920 H.264 with audio. The render log is
also checked, so an FFmpeg error fails the test even if a file was produced.
"""
import json
import subprocess
from pathlib import Path

import pytest

from app.fonts import DEFAULT_FONT_STYLE, get_font_style
from app.subtitles import generate_ass_subtitles_for_clip
from app.video import compute_smart_crop, find_ffmpeg_executable, render_vertical_clip

# "split" is tested separately: it needs a background clip in backgrounds/.
VIDEO_STYLES = ["smart_fit", "blurred", "solid", "gradient"]

HOOK_TITLE = 'He said "wow": 100% 🚀 it\'s {fine}'


def _ffprobe_for(ffmpeg: str) -> str:
    """ffprobe sits next to ffmpeg in the usual builds."""
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe")
    return str(sibling) if sibling.exists() else "ffprobe"


def _make_source_video(ffmpeg: str, path: Path) -> None:
    """A tiny horizontal clip with audio, generated locally."""
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=s=640x360:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        timeout=120,
    )


def _sample_transcript():
    return [{
        "start": 0.0,
        "end": 2.0,
        "text": "hello world",
        "words": [
            {"word": "hello", "start": 0.1, "end": 0.7},
            {"word": "world", "start": 0.8, "end": 1.4},
        ],
    }]


@pytest.mark.parametrize("video_style", VIDEO_STYLES)
def test_video_style_renders_1080x1920_h264_with_audio(video_style, tmp_path):
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")

    source = tmp_path / "input.mp4"
    _make_source_video(ffmpeg, source)

    style = get_font_style(DEFAULT_FONT_STYLE)
    ass_path = tmp_path / "sample.ass"
    generate_ass_subtitles_for_clip(
        transcript_segments=_sample_transcript(),
        clip_start=0.0,
        clip_end=1.5,
        output_ass_path=ass_path,
        font_style=style,
        hook_title=HOOK_TITLE,
        hook_duration=3,
        progress_bar=True,
        progress_color="#38BDF8",
    )

    output = tmp_path / f"{video_style}.mp4"
    log_path = tmp_path / f"{video_style}.log"
    render_vertical_clip(
        input_video_path=source,
        start_time=0.0,
        end_time=1.5,
        output_mp4_path=output,
        subtitle_ass_path=ass_path,
        font_filename=style["filename"],
        video_style=video_style,
        solid_color="#1D4ED8",
        gradient_top="#6D28D9",
        gradient_bottom="#1E1B4B",
        log_path=log_path,
    )

    assert output.exists() and output.stat().st_size > 10_000

    # FFmpeg must not log an error, even when it exits with 0.
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    errors = [line for line in log_text.splitlines() if "error" in line.lower()]
    assert not errors, "FFmpeg reported errors:\n" + "\n".join(errors[:10])

    probe = subprocess.run(
        [
            _ffprobe_for(ffmpeg), "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height",
            "-of", "json", str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    streams = json.loads(probe.stdout)["streams"]

    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    assert video, "no video stream"
    assert video[0]["codec_name"] == "h264"
    assert video[0]["width"] == 1080
    assert video[0]["height"] == 1920
    assert audio, "the clip must keep its audio track"


def test_hook_title_is_escaped_and_captions_stay_in_lower_third(tmp_path):
    style = get_font_style("plain")  # Plain still has to sit in the safe area
    ass_path = tmp_path / "hook.ass"
    generate_ass_subtitles_for_clip(
        transcript_segments=_sample_transcript(),
        clip_start=0.0,
        clip_end=1.5,
        output_ass_path=ass_path,
        font_style=style,
        hook_title=HOOK_TITLE,
        hook_duration=3,
    )
    content = ass_path.read_text(encoding="utf-8")

    # A top hook style and its event with a fade.
    assert "Style: Hook," in content
    assert "Dialogue: 1,0:00:00.00" in content
    assert r"\fad(300,300)" in content

    # Quotes, colon, percent, apostrophe and emoji survive; braces are neutralized.
    assert 'He said "wow": 100% 🚀 it\'s (fine)' in content

    # The caption style is pinned to the lower third (MarginV 500).
    assert ",2,90,90,500,1" in content


# ---------------------------------------------------------------------------
# Smart fit
# ---------------------------------------------------------------------------

def _log_errors(log_path: Path) -> list:
    """Every FFmpeg log line that mentions an error."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return [line for line in text.splitlines() if "error" in line.lower()]


def _command_line(log_path: Path) -> str:
    """The exact FFmpeg command, saved as the first line of every render log."""
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# Command:"):
            return line
    return ""


def _probe_streams(ffmpeg: str, video: Path) -> list:
    probe = subprocess.run(
        [
            _ffprobe_for(ffmpeg), "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height",
            "-of", "json", str(video),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    return json.loads(probe.stdout)["streams"]


def _assert_vertical_h264_with_audio(ffmpeg: str, video: Path) -> None:
    streams = _probe_streams(ffmpeg, video)
    pictures = [s for s in streams if s.get("codec_type") == "video"]
    sound = [s for s in streams if s.get("codec_type") == "audio"]
    assert pictures, "no video stream"
    assert pictures[0]["codec_name"] == "h264"
    assert pictures[0]["width"] == 1080
    assert pictures[0]["height"] == 1920
    assert sound, "the clip must keep its audio track"


def _pixel(ffmpeg: str, video: Path, x: int, y: int, moment: float = 0.6) -> tuple:
    """Average colour of a small square, read straight out of the clip."""
    raw = subprocess.run(
        [
            ffmpeg, "-v", "error", "-ss", str(moment), "-i", str(video),
            "-vf", f"crop=8:8:{x}:{y},scale=1:1", "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        timeout=60,
    ).stdout
    assert len(raw) >= 3, "could not read a pixel from the rendered clip"
    return tuple(raw[:3])


def _make_static_source(ffmpeg: str, path: Path) -> None:
    """Black clip with a red square, so a slow zoom can be measured."""
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", "color=c=black:s=640x360:r=30:d=2,drawbox=x=200:y=150:w=50:h=50:color=red@1:t=fill",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        timeout=120,
    )


def _red_pixels(ffmpeg: str, video: Path, moment: float) -> int:
    """How many pixels of one frame are red (the square in the static clip)."""
    import numpy as np

    raw = subprocess.run(
        [
            ffmpeg, "-v", "error", "-ss", str(moment), "-i", str(video),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        timeout=120,
    ).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(int)
    red = (frame[:, 0] > 150) & (frame[:, 1] < 90) & (frame[:, 2] < 90)
    return int(red.sum())


def test_smart_crop_window_follows_the_face_and_stays_inside():
    """The crop is one static window: around the face, clamped to the frame."""
    # 640x360 source, 4:5 crop -> 288x360, centered without a face.
    assert compute_smart_crop(640, 360, None, 5 / 4) == (288, 360, 176, 0)
    # A face near the left edge moves the window to the left, without leaving the frame.
    assert compute_smart_crop(640, 360, 60.0, 5 / 4)[2] == 0
    assert compute_smart_crop(640, 360, 600.0, 5 / 4)[2] == 352
    # Taller shapes (1:1 and 3:4) crop less of the width.
    assert compute_smart_crop(640, 360, None, 1.0)[:2] == (360, 360)
    assert compute_smart_crop(640, 360, None, 4 / 3)[:2] == (270, 360)
    # A portrait source keeps its full width and only trims the height.
    assert compute_smart_crop(720, 1280, None, 1.0) == (720, 720, 0, 280)


@pytest.mark.parametrize(
    "face_x, expected_crop",
    [(60.0, "crop=288:360:0:0"), (None, "crop=288:360:176:0")],
    ids=["with-face", "no-face"],
)
def test_smart_fit_renders_with_and_without_a_face(face_x, expected_crop, tmp_path, monkeypatch):
    """
    Haar finds no faces in generated footage, so the detector result is stubbed
    here: one clip with a face (left crop) and one without (centered crop).
    Both must render 1080x1920 H.264 with audio and no FFmpeg errors.
    """
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")

    monkeypatch.setattr("app.video.detect_face_center_x", lambda *a, **k: face_x)

    source = tmp_path / "input.mp4"
    _make_source_video(ffmpeg, source)

    output = tmp_path / "smart.mp4"
    log_path = tmp_path / "smart.log"
    render_vertical_clip(
        input_video_path=source,
        start_time=0.0,
        end_time=1.5,
        output_mp4_path=output,
        log_path=log_path,
    )

    assert output.exists() and output.stat().st_size > 10_000
    _assert_vertical_h264_with_audio(ffmpeg, output)

    errors = _log_errors(log_path)
    assert not errors, "FFmpeg reported errors:\n" + "\n".join(errors[:10])

    # The detected face really was used for the (single) crop window.
    assert expected_crop in _command_line(log_path)


def test_smart_fit_punch_in_zoom_makes_the_picture_grow(tmp_path):
    """The optional punch-in goes from 1.00 to 1.06 over the clip."""
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")

    source = tmp_path / "static.mp4"
    _make_static_source(ffmpeg, source)

    output = tmp_path / "zoom.mp4"
    log_path = tmp_path / "zoom.log"
    render_vertical_clip(
        input_video_path=source,
        start_time=0.0,
        end_time=1.5,
        output_mp4_path=output,
        log_path=log_path,
        smart_zoom=True,
    )

    _assert_vertical_h264_with_audio(ffmpeg, output)
    errors = _log_errors(log_path)
    assert not errors, "FFmpeg reported errors:\n" + "\n".join(errors[:10])

    start = _red_pixels(ffmpeg, output, 0.05)
    end = _red_pixels(ffmpeg, output, 1.4)
    assert start > 0 and end > start * 1.05, f"no punch-in: {start} -> {end}"


def test_smart_fit_rounds_the_corners_and_draws_the_shadow(tmp_path):
    """A solid background makes corners and shadow easy to measure."""
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")

    source = tmp_path / "input.mp4"
    _make_source_video(ffmpeg, source)

    output = tmp_path / "rounded.mp4"
    log_path = tmp_path / "rounded.log"
    render_vertical_clip(
        input_video_path=source,
        start_time=0.0,
        end_time=1.5,
        output_mp4_path=output,
        log_path=log_path,
        smart_face_x=320.0,
        smart_background="solid",
        solid_color="#1D4ED8",
    )

    _assert_vertical_h264_with_audio(ffmpeg, output)
    errors = _log_errors(log_path)
    assert not errors, "FFmpeg reported errors:\n" + "\n".join(errors[:10])

    background = (29, 78, 216)   # #1D4ED8

    # 4:5 block is 1350 tall, so it spans y=285..1635 in a 1920 frame.
    corner = _pixel(ffmpeg, output, 2, 287)
    assert abs(sum(corner) - sum(background)) < 70, f"corner is not rounded: {corner}"

    above = _pixel(ffmpeg, output, 540, 200)
    assert abs(sum(above) - sum(background)) < 20, f"background above the block is wrong: {above}"

    shadow = _pixel(ffmpeg, output, 540, 1650)
    assert sum(shadow) < sum(background) - 25, f"no soft shadow under the block: {shadow}"


# ---------------------------------------------------------------------------
# Split screen
# ---------------------------------------------------------------------------

def _make_background_clip(ffmpeg: str, path: Path, seconds: float = 1.0) -> None:
    """
    A synthetic background with its own audio track, so we can prove the
    background's sound is dropped. One second is shorter than the clip we
    render, which also proves the loop.
    """
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=s=1280x720:r=25:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        timeout=120,
    )


def _make_solid_source(ffmpeg: str, path: Path, color: str = "0xFF8000") -> None:
    """One flat colour with audio, so the two halves are easy to tell apart."""
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=30:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        timeout=120,
    )


def _probe_duration(ffmpeg: str, video: Path) -> float:
    probe = subprocess.run(
        [
            _ffprobe_for(ffmpeg), "-v", "error",
            "-show_entries", "format=duration", "-of", "json", str(video),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    return float(json.loads(probe.stdout)["format"]["duration"])


def test_split_screen_loops_a_synthetic_background(tmp_path, monkeypatch):
    """
    A 1 s background under a 1.5 s clip: FFmpeg must loop it, cut it to the clip
    length, drop its audio, and still produce 1080x1920 H.264 with exactly one
    audio stream and no error lines.
    """
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")

    # Never touch the user's real backgrounds/ folder.
    folder = tmp_path / "backgrounds"
    folder.mkdir()
    monkeypatch.setattr("app.backgrounds.BACKGROUNDS_DIR", folder)

    background = folder / "loop.mp4"
    _make_background_clip(ffmpeg, background, seconds=1.0)

    source = tmp_path / "input.mp4"
    _make_source_video(ffmpeg, source)

    output = tmp_path / "split.mp4"
    log_path = tmp_path / "split.log"
    render_vertical_clip(
        input_video_path=source,
        start_time=0.0,
        end_time=1.5,
        output_mp4_path=output,
        log_path=log_path,
        video_style="split",
        smart_face_x=320.0,
        split_background="loop.mp4",
    )

    assert output.exists() and output.stat().st_size > 10_000
    _assert_vertical_h264_with_audio(ffmpeg, output)

    streams = _probe_streams(ffmpeg, output)
    sound = [s for s in streams if s.get("codec_type") == "audio"]
    assert len(sound) == 1, f"expected one audio stream, got {len(sound)}"

    errors = _log_errors(log_path)
    assert not errors, "FFmpeg reported errors:\n" + "\n".join(errors[:10])

    command = _command_line(log_path)
    assert "-stream_loop" in command, "the background must be looped"
    assert "loop.mp4" in command

    # The 1 s background was cut to the clip length, not left at 1 s.
    assert _probe_duration(ffmpeg, output) >= 1.4


def test_split_screen_layout_and_optional_seam_line(tmp_path, monkeypatch):
    """
    Top: the main clip from y=0 with rounded corners (a background pixel shows
    in the corner). Bottom: the background clip. The seam line is opt-in.
    """
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")

    folder = tmp_path / "backgrounds"
    folder.mkdir()
    monkeypatch.setattr("app.backgrounds.BACKGROUNDS_DIR", folder)

    # Pure black background: the bottom half is then easy to recognise.
    background = folder / "black.mp4"
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(background),
        ],
        check=True,
        timeout=120,
    )

    source = tmp_path / "orange.mp4"
    _make_solid_source(ffmpeg, source)

    outputs = {}
    for label, divider in (("line", True), ("noline", False)):
        output = tmp_path / f"{label}.mp4"
        log_path = tmp_path / f"{label}.log"
        render_vertical_clip(
            input_video_path=source,
            start_time=0.0,
            end_time=1.5,
            output_mp4_path=output,
            log_path=log_path,
            video_style="split",
            smart_face_x=320.0,
            split_background="black.mp4",
            split_divider=divider,
        )
        _assert_vertical_h264_with_audio(ffmpeg, output)
        errors = _log_errors(log_path)
        assert not errors, "FFmpeg reported errors:\n" + "\n".join(errors[:10])
        outputs[label] = output

    picture = outputs["line"]

    # The block starts at the very top of the frame.
    top = _pixel(ffmpeg, picture, 540, 20)
    assert sum(top) > 300, f"the main clip is not at the top: {top}"

    # The top block ends at the seam (58% of 1920 is about 1114).
    above = _pixel(ffmpeg, picture, 540, 1100)
    below = _pixel(ffmpeg, picture, 540, 1600)
    assert sum(above) > 300, f"the main clip is missing above the seam: {above}"
    assert sum(below) < 60, f"the background is missing below the seam: {below}"

    # A rounded corner lets the background show through.
    corner = _pixel(ffmpeg, picture, 2, 2)
    assert sum(corner) < 60, f"the corner is not rounded: {corner}"

    # The thin seam line is only drawn when it is switched on.
    assert "drawbox=x=0:y=1112" in _command_line(tmp_path / "line.log")
    assert "drawbox" not in _command_line(tmp_path / "noline.log")


def test_split_captions_sit_at_the_seam_not_on_the_face():
    """Split screen moves captions up to the seam; other styles are untouched."""
    from app.jobs import SPLIT_CAPTION_MARGIN_V, _caption_style_for

    style = get_font_style(DEFAULT_FONT_STYLE)
    assert style["margin_v"] != SPLIT_CAPTION_MARGIN_V

    moved = _caption_style_for(style, {"video_style": "split"})
    assert moved["margin_v"] == SPLIT_CAPTION_MARGIN_V
    # The shared font style must not be modified for other clips.
    assert style["margin_v"] != SPLIT_CAPTION_MARGIN_V

    assert _caption_style_for(style, {"video_style": "smart_fit"}) is style


def test_backgrounds_folder_defaults_and_upload_names(tmp_path, monkeypatch):
    """backgrounds/ helpers: safe names, no escape, 60 s limit, random pick."""
    from app import backgrounds

    folder = tmp_path / "backgrounds"
    monkeypatch.setattr(backgrounds, "BACKGROUNDS_DIR", folder)

    # Empty folder: nothing to pick, so the render falls back to a dark canvas.
    assert backgrounds.resolve_background("random") is None
    assert backgrounds.resolve_background(None) is None
    assert backgrounds.resolve_background("none") is None
    assert backgrounds.list_backgrounds() == []

    # Upload names are cleaned and can never leave backgrounds/.
    assert backgrounds.safe_background_name("../../evil clip.MP4") == "evil_clip.mp4"
    assert backgrounds.safe_background_name("no-extension") == "no-extension.mp4"

    fake = folder / "fake.mp4"
    fake.write_bytes(b"not a video")
    assert backgrounds.check_background(fake) is not None
    assert backgrounds.resolve_background("fake.mp4") == fake

    # A valid but very long clip is rejected (limit is 60 seconds).
    ffmpeg = find_ffmpeg_executable()
    if ffmpeg:
        long_clip = folder / "long.mp4"
        subprocess.run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=s=64x64:r=5:d=61",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(long_clip),
            ],
            check=True,
            timeout=180,
        )
        message = backgrounds.check_background(long_clip)
        assert message and "60 seconds" in message


def test_split_settings_and_background_upload_api(tmp_path, monkeypatch):
    """The results page and the two endpoints keep Split screen in sync."""
    from fastapi.testclient import TestClient

    import app.main as main_module
    from app import backgrounds

    folder = tmp_path / "backgrounds"
    folder.mkdir()
    monkeypatch.setattr(backgrounds, "BACKGROUNDS_DIR", folder)

    fake_job = {
        "job_id": "job_test",
        "original_filename": "x.mp4",
        "status": "completed",
        "progress_percent": 100,
        "progress_message": "done",
        "candidates": [],
        "font_style": DEFAULT_FONT_STYLE,
        "video_style": "split",
    }
    saved = {}
    monkeypatch.setattr(main_module, "load_job", lambda job_id: dict(fake_job))
    monkeypatch.setattr(main_module, "save_job", lambda job_id, data: saved.update(data))

    client = TestClient(main_module.app)

    page = client.get("/jobs/job_test/view")
    assert page.status_code == 200
    for expected in (
        'value="split"',
        'id="splitControls"',
        'id="splitBackground"',
        'id="splitDivider"',
        "licence allowing reuse",
    ):
        assert expected in page.text, f"missing from the results page: {expected}"

    # Settings round-trip.
    resp = client.post(
        "/api/jobs/job_test/video-style",
        json={"video_style": "split", "split_background": "random", "split_divider": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["video_style"] == "split"
    assert body["split_background"] == "random"
    assert body["split_divider"] is False
    assert saved["split_background"] == "random"

    # "No clip (dark)" is a valid choice too.
    resp = client.post("/api/jobs/job_test/video-style", json={"split_background": "none"})
    assert resp.status_code == 200
    assert resp.json()["split_background"] == "none"

    # An unknown background name is refused.
    resp = client.post(
        "/api/jobs/job_test/video-style",
        json={"split_background": "not-in-the-folder.mp4"},
    )
    assert resp.status_code == 400

    # A file that ffprobe cannot read is refused and never reaches backgrounds/.
    resp = client.post(
        "/api/backgrounds/upload",
        files={"file": ("bad.mp4", b"not a video", "video/mp4")},
    )
    assert resp.status_code == 400
    assert not any(folder.iterdir())

    # The wrong extension is refused before anything is written.
    resp = client.post(
        "/api/backgrounds/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400

    # A real clip is accepted, tidied and appears in the list.
    ffmpeg = find_ffmpeg_executable()
    if ffmpeg:
        clip = tmp_path / "ok.mp4"
        _make_background_clip(ffmpeg, clip, seconds=1.0)
        resp = client.post(
            "/api/backgrounds/upload",
            files={"file": ("my clip.mp4", clip.read_bytes(), "video/mp4")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "my_clip.mp4"
        names = [item["name"] for item in body["backgrounds"]]
        assert "my_clip.mp4" in names

        listed = client.get("/api/backgrounds").json()["backgrounds"]
        assert [item["name"] for item in listed] == names

