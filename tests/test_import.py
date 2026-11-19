"""
Tests for "Import from link": link conversion, SSRF blocking, downloading,
size/duration limits, and the upload page UI.
"""
import shutil
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from app import downloader, jobs
from app.config import (
    BASE_DIR,
    JOBS_DIR,
    MAX_FILE_SIZE_BYTES,
    MAX_VIDEO_MINUTES,
    YTDLP_MAX_HEIGHT,
)
from app.downloader import (
    DownloadResult,
    LinkDownloadError,
    LinkError,
    LinkTooLargeError,
    UnsafeLinkError,
    UnsupportedLinkError,
    _cookies_from_browser,
    build_ytdlp_options,
    check_ytdlp_runtime,
    convert_share_link,
    download_from_link,
    download_video,
    download_with_ytdlp,
    friendly_ytdlp_error,
    is_public_ip,
    pick_extension,
    validate_public_url,
)
from app.main import app
from app import main as app_module
from app.video import probe_video_file

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fake HTTP objects so the tests never touch the real internet
# ---------------------------------------------------------------------------

class FakeResponse:
    """Looks just enough like a requests.Response for the downloader."""

    def __init__(self, status_code=200, headers=None, chunks=None, error=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._error = error
        self.closed = False

    def iter_content(self, chunk_size):
        for chunk in self._chunks:
            yield chunk
        if self._error:
            raise self._error

    def close(self):
        self.closed = True


class FakeSession:
    """Returns queued responses in order (one per request)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requested_urls = []

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        return self._responses.pop(0)


def allow_all(url):
    """Per-file validator used when a test deliberately talks to localhost."""
    return None


# ---------------------------------------------------------------------------
# Turning share links into direct download links
# ---------------------------------------------------------------------------

def test_google_drive_view_link_becomes_direct_download():
    direct, provider = convert_share_link(
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz12345/view?usp=sharing"
    )
    assert provider == "google_drive"
    assert direct.startswith("https://drive.usercontent.google.com/download")
    assert "id=1AbCdEfGhIjKlMnOpQrStUvWxYz12345" in direct
    assert "export=download" in direct


def test_google_drive_open_id_link_becomes_direct_download():
    direct, provider = convert_share_link("https://drive.google.com/open?id=ABCdef1234567890")
    assert provider == "google_drive"
    assert "id=ABCdef1234567890" in direct


def test_google_drive_link_without_file_id_is_rejected():
    with pytest.raises(UnsupportedLinkError) as err:
        convert_share_link("https://drive.google.com/drive/folders/abc123")
    assert "file id" in str(err.value).lower()


def test_dropbox_share_link_gets_dl_1():
    direct, provider = convert_share_link("https://www.dropbox.com/s/abc123/my%20clip.mp4?dl=0")
    assert provider == "dropbox"
    assert "dl=1" in direct
    assert "dl=0" not in direct


def test_dropbox_link_keeps_other_parameters():
    direct, _provider = convert_share_link(
        "https://www.dropbox.com/scl/fi/xyz/clip.mov?rlkey=keepme&dl=0"
    )
    assert "rlkey=keepme" in direct
    assert "dl=1" in direct


def test_direct_video_link_is_kept_as_is():
    url = "https://cdn.example.com/videos/holiday.mp4?token=abc"
    direct, provider = convert_share_link(url)
    assert direct == url
    assert provider == "direct"


@pytest.mark.parametrize("url,provider", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/shorts/abc123", "youtube"),
    ("https://m.youtube.com/watch?v=abc", "youtube"),
    ("https://www.tiktok.com/@someone/video/123", "tiktok"),
    ("https://vm.tiktok.com/ZMabc/", "tiktok"),
    ("https://www.instagram.com/reel/abc123/", "instagram"),
    ("https://instagr.am/p/abc123/", "instagram"),
])
def test_streaming_links_are_handed_to_ytdlp(url, provider):
    target, detected = convert_share_link(url)
    assert detected == provider
    # yt-dlp needs the page URL, so we keep it instead of guessing a file URL.
    assert target == url


@pytest.mark.parametrize("url", [
    "https://vimeo.com/123456789",
    "https://www.facebook.com/watch/?v=123",
    "https://www.twitch.tv/someone",
])
def test_other_streaming_sites_are_rejected(url):
    with pytest.raises(UnsupportedLinkError) as err:
        convert_share_link(url)
    assert "not supported" in str(err.value).lower()


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PL123456789",
    "https://www.youtube.com/watch?list=PL123456789",
])
def test_playlist_links_are_rejected(url):
    """A playlist would download every video into one file, so refuse it."""
    with pytest.raises(UnsupportedLinkError) as err:
        convert_share_link(url)
    assert "single video" in str(err.value).lower()


def test_video_link_with_playlist_is_still_allowed():
    """watch?v=... &list=... is one video, not a playlist."""
    url = "https://www.youtube.com/watch?v=abc123&list=PL123456789"
    target, provider = convert_share_link(url)
    assert target == url
    assert provider == "youtube"


def test_non_video_file_is_rejected():
    with pytest.raises(UnsupportedLinkError) as err:
        convert_share_link("https://example.com/downloads/archive.zip")
    assert ".zip" in str(err.value)


@pytest.mark.parametrize("url", ["ftp://example.com/a.mp4", "file:///C:/a.mp4", "javascript:alert(1)", ""])
def test_non_http_links_are_rejected(url):
    with pytest.raises(UnsupportedLinkError):
        convert_share_link(url)


# ---------------------------------------------------------------------------
# Private / local network blocking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1",          # loopback
    "10.1.2.3",           # 10.0.0.0/8
    "172.16.0.1",         # 172.16.0.0/12
    "172.31.255.254",
    "192.168.1.10",       # 192.168.0.0/16
    "169.254.169.254",    # link-local / cloud metadata
    "0.0.0.0",
    "::1",
    "fe80::1",
    "fc00::1",
])
def test_private_addresses_are_not_public(ip):
    assert is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_addresses_are_allowed(ip):
    assert is_public_ip(ip) is True


def test_validate_blocks_literal_localhost():
    with pytest.raises(UnsafeLinkError) as err:
        validate_public_url("http://127.0.0.1/video.mp4")
    assert "private" in str(err.value).lower()


def test_validate_blocks_private_hostname():
    with pytest.raises(UnsafeLinkError) as err:
        validate_public_url("http://internal.example/video.mp4", resolver=lambda host: ["192.168.0.5"])
    assert "private" in str(err.value).lower()


def test_validate_allows_public_host():
    # No exception means the link is acceptable.
    validate_public_url("https://example.com/video.mp4", resolver=lambda host: ["93.184.216.34"])


def test_validate_blocks_unusual_port():
    with pytest.raises(UnsafeLinkError) as err:
        validate_public_url("http://example.com:6379/video.mp4", resolver=lambda host: ["93.184.216.34"])
    assert "port" in str(err.value).lower()


def test_redirect_to_private_address_is_blocked(tmp_path):
    """A public link must not be able to bounce the server to 127.0.0.1."""
    private_hop = FakeResponse(302, headers={"Location": "http://127.0.0.1/secret.mp4"})
    session = FakeSession([private_hop])
    seen_hosts = []

    def resolver(host):
        seen_hosts.append(host)
        return ["93.184.216.34"] if host == "example.com" else ["127.0.0.1"]

    with pytest.raises(UnsafeLinkError):
        download_video(
            "https://example.com/video.mp4",
            tmp_path,
            validate_url=lambda url: validate_public_url(url, resolver=resolver),
            session=session,
        )

    # Both hops were checked: the public one, then the private redirect target.
    assert seen_hosts == ["example.com", "127.0.0.1"]


# ---------------------------------------------------------------------------
# File type, size and friendly errors
# ---------------------------------------------------------------------------

def test_pick_extension_from_url_header_and_content_type():
    assert pick_extension("https://x.test/a.MP4") == ".mp4"
    assert pick_extension(
        "https://x.test/download",
        content_disposition='attachment; filename="my clip.mkv"',
    ) == ".mkv"
    assert pick_extension("https://x.test/download", content_type="video/webm") == ".webm"
    assert pick_extension("https://x.test/download", content_type="text/html") is None


def test_content_length_over_limit_is_rejected(tmp_path):
    oversize = str(MAX_FILE_SIZE_BYTES + 1024)
    response = FakeResponse(200, headers={"Content-Type": "video/mp4", "Content-Length": oversize})
    with pytest.raises(LinkTooLargeError) as err:
        download_video(
            "https://example.com/big.mp4",
            tmp_path,
            validate_url=allow_all,
            session=FakeSession([response]),
        )
    assert "limit" in str(err.value).lower()
    assert not list(tmp_path.glob("input.*"))


def test_share_page_html_gives_friendly_error(tmp_path):
    response = FakeResponse(200, headers={"Content-Type": "text/html; charset=utf-8"})
    with pytest.raises(LinkDownloadError) as err:
        download_video(
            "https://drive.google.com/file/d/abc/view",
            tmp_path,
            validate_url=allow_all,
            session=FakeSession([response]),
        )
    message = str(err.value).lower()
    assert "web page" in message
    assert "anyone with the link" in message


def test_broken_download_leaves_no_partial_file(tmp_path):
    response = FakeResponse(
        200,
        headers={"Content-Type": "video/mp4"},
        chunks=[b"x" * 1024],
        error=requests.ConnectionError("connection dropped"),
    )
    with pytest.raises(LinkDownloadError):
        download_video(
            "https://example.com/video.mp4",
            tmp_path,
            validate_url=allow_all,
            session=FakeSession([response]),
        )
    assert not list(tmp_path.glob("input.*"))


def test_http_error_is_friendly(tmp_path):
    response = FakeResponse(404, headers={"Content-Type": "text/html"})
    with pytest.raises(LinkDownloadError) as err:
        download_video(
            "https://example.com/missing.mp4",
            tmp_path,
            validate_url=allow_all,
            session=FakeSession([response]),
        )
    assert "not found" in str(err.value).lower()


# ---------------------------------------------------------------------------
# Full download of a real video from a local web server
# ---------------------------------------------------------------------------

def test_downloads_real_video_from_web_server(tmp_path):
    """End to end: serve sample_video.mp4 over HTTP, download it, and ffprobe it."""
    sample = BASE_DIR / "sample_video.mp4"
    if not sample.exists():
        pytest.skip("sample_video.mp4 is missing")

    handler = partial(SimpleHTTPRequestHandler, directory=str(BASE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    progress = []
    try:
        result = download_video(
            f"http://127.0.0.1:{port}/sample_video.mp4",
            tmp_path,
            validate_url=allow_all,  # this test talks to localhost on purpose
            progress_cb=lambda done, total: progress.append((done, total)),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert isinstance(result, DownloadResult)
    assert result.extension == ".mp4"
    assert result.path.exists()
    assert result.path.stat().st_size == sample.stat().st_size
    assert result.size_bytes == sample.stat().st_size
    assert progress, "progress callback was never called"

    # The real proof: ffprobe agrees it is a video with a length.
    info = probe_video_file(result.path)
    assert info["duration"] > 0
    assert info["width"] > 0
    assert info["height"] > 0


def test_probe_rejects_non_video_file(tmp_path):
    fake = tmp_path / "not_a_video.mp4"
    fake.write_bytes(b"this is definitely not a video" * 100)
    with pytest.raises(Exception) as err:
        probe_video_file(fake)
    assert "not a video" in str(err.value).lower() or "damaged" in str(err.value).lower()


# ---------------------------------------------------------------------------
# Streaming sites: yt-dlp options, error messages and download
# ---------------------------------------------------------------------------

def test_ytdlp_options_cap_quality_and_size(tmp_path, monkeypatch):
    """720p mp4 by default, merged to MP4, with fast fragment downloads."""
    monkeypatch.setattr(downloader, "YTDLP_COOKIES_FILE", "")
    monkeypatch.setattr(downloader, "YTDLP_COOKIES_FROM_BROWSER", "")
    monkeypatch.setattr(downloader, "find_ffmpeg_executable", lambda: None)
    monkeypatch.setattr(downloader.shutil, "which", lambda name: None)

    options = build_ytdlp_options(tmp_path)

    assert f"height<={YTDLP_MAX_HEIGHT}" in options["format"]
    assert "avc1" in options["format"]
    assert "m4a" in options["format"]
    assert options["merge_output_format"] == "mp4"
    assert "max_filesize" not in options, "size is checked with a clear message instead"
    assert options["noplaylist"] is True
    assert options["concurrent_fragment_downloads"] == 4
    assert options["socket_timeout"] == 15
    assert options["retries"] == 3
    assert options["fragment_retries"] == 3
    assert options["outtmpl"] == str(tmp_path / "input.%(ext)s")
    # Signed-out by default: no cookies are read unless the user asks for it.
    assert "cookiefile" not in options
    assert "cookiesfrombrowser" not in options


def test_ytdlp_options_add_cookies_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "YTDLP_COOKIES_FILE", r"C:\temp\cookies.txt")
    monkeypatch.setattr(downloader, "YTDLP_COOKIES_FROM_BROWSER", "chrome:Profile 1")

    options = build_ytdlp_options(tmp_path)

    assert options["cookiefile"] == r"C:\temp\cookies.txt"
    assert options["cookiesfrombrowser"] == ("chrome", "Profile 1")


def test_cookies_from_browser_parsing():
    assert _cookies_from_browser("") is None
    assert _cookies_from_browser("   ") is None
    assert _cookies_from_browser("edge") == ("edge",)
    assert _cookies_from_browser("chrome:Profile 1") == ("chrome", "Profile 1")


def test_ytdlp_options_use_ffmpeg_and_node_when_available(tmp_path, monkeypatch):
    """ffmpeg is passed explicitly, and Node is enabled only when it exists."""
    monkeypatch.setattr(downloader, "find_ffmpeg_executable", lambda: r"C:\ffmpeg\bin\ffmpeg.exe")
    monkeypatch.setattr(
        downloader.shutil,
        "which",
        lambda name: r"C:\Program Files\nodejs\node.exe" if name == "node" else None,
    )

    options = build_ytdlp_options(tmp_path)

    assert options["ffmpeg_location"] == r"C:\ffmpeg\bin\ffmpeg.exe"
    assert options["js_runtimes"] == {"node": {"path": r"C:\Program Files\nodejs\node.exe"}}


def test_ytdlp_options_skip_node_when_not_installed(tmp_path, monkeypatch):
    """No Node on this computer: leave the JS runtime option out instead of failing."""
    monkeypatch.setattr(downloader, "find_ffmpeg_executable", lambda: None)
    monkeypatch.setattr(downloader.shutil, "which", lambda name: None)

    options = build_ytdlp_options(tmp_path)

    assert "ffmpeg_location" not in options
    assert "js_runtimes" not in options


def test_runtime_preflight_is_ok_with_node_22_and_ejs(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda name: r"C:\Program Files\nodejs\node.exe")
    monkeypatch.setattr(downloader, "_node_major_version", lambda path: 24)
    monkeypatch.setattr(downloader.importlib.util, "find_spec", lambda name: object())

    assert check_ytdlp_runtime() is None


def test_runtime_preflight_needs_node(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda name: None)

    problem = check_ytdlp_runtime()

    assert "Node.js 22" in problem
    assert 'pip install -U "yt-dlp[default]"' in problem


def test_runtime_preflight_needs_newer_node(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda name: r"C:\Program Files\nodejs\node.exe")
    monkeypatch.setattr(downloader, "_node_major_version", lambda path: 18)

    problem = check_ytdlp_runtime()

    assert "v18" in problem
    assert 'pip install -U "yt-dlp[default]"' in problem


def test_runtime_preflight_needs_yt_dlp_ejs(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda name: r"C:\Program Files\nodejs\node.exe")
    monkeypatch.setattr(downloader, "_node_major_version", lambda path: 24)
    monkeypatch.setattr(downloader.importlib.util, "find_spec", lambda name: None)

    problem = check_ytdlp_runtime()

    assert "yt_dlp_ejs" in problem
    assert 'pip install -U "yt-dlp[default]"' in problem


@pytest.mark.parametrize("raw,expected", [
    ("ERROR: Sign in to confirm you're not a bot", "bot"),
    ("ERROR: Unable to solve JS challenge", "safety check"),
    ("ERROR: Requested format is not available", "ytdlp_max_height"),
    ("ERROR: This video is not available in your country", "region"),
    ("ERROR: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: This video is a playlist", "playlist"),
    ("ERROR: Live stream recording", "live"),
    ("HTTP Error 429: Too Many Requests", "rate-limiting"),
    ("ERROR: Unsupported URL", "not a video page"),
    ("ERROR: Video unavailable", "unavailable"),
    ("something totally unexpected", "could not be downloaded"),
])
def test_friendly_ytdlp_error_messages(raw, expected):
    assert expected in friendly_ytdlp_error(raw).lower()


def test_download_from_link_sends_streaming_links_to_ytdlp(monkeypatch, tmp_path):
    fake_result = DownloadResult(
        tmp_path / "input.mp4", 10, "https://youtu.be/abc", "video/mp4", ".mp4", provider="youtube"
    )
    calls = []

    def fake_ytdlp(url, dest_dir, provider=None, progress_cb=None, stage_cb=None):
        calls.append((url, provider, dest_dir))
        return fake_result

    monkeypatch.setattr(downloader, "download_with_ytdlp", fake_ytdlp)

    result = download_from_link("https://youtu.be/abc", tmp_path)

    assert calls == [("https://youtu.be/abc", "youtube", tmp_path)]
    assert result is fake_result


def test_download_from_link_uses_plain_downloader_for_direct_links(monkeypatch, tmp_path):
    calls = []

    def fake_download(url, dest_dir, filename_stem="input", progress_cb=None):
        calls.append(url)
        return DownloadResult(dest_dir / "input.mp4", 10, url, "video/mp4", ".mp4")

    monkeypatch.setattr(downloader, "download_video", fake_download)

    result = download_from_link("https://cdn.example.com/clip.mp4", tmp_path)

    assert calls == ["https://cdn.example.com/clip.mp4"]
    # The provider from convert_share_link is stamped onto the result.
    assert result.provider == "direct"


class _FakeYoutubeDLBase:
    """Shared shape of yt-dlp's YoutubeDL, so no real network call happens."""

    def __init__(self, options):
        self.options = options
        self.info = {"id": "abc"}
        self.processed = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        # One lookup result, reused by the download step.
        return self.info

    def process_ie_result(self, info, download=False):
        self.processed = info


def test_download_with_ytdlp_reuses_one_info_and_shows_stages(monkeypatch, tmp_path):
    stages = []
    instances = []

    class FakeYoutubeDL(_FakeYoutubeDLBase):
        def process_ie_result(self, info, download=False):
            super().process_ie_result(info, download=download)
            hook = self.options["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": 512,
                  "total_bytes": 1024, "speed": 3.2 * 1024 * 1024})
            pp = self.options["postprocessor_hooks"][0]
            pp({"status": "started", "postprocessor": "Merger"})
            pp({"status": "finished", "postprocessor": "Merger"})
            hook({"status": "finished", "downloaded_bytes": 1024, "total_bytes": 1024})
            Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"x" * 1024)

    def fake_youtubedl(options):
        instance = FakeYoutubeDL(options)
        instances.append(instance)
        return instance

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", fake_youtubedl)

    result = download_with_ytdlp(
        "https://youtu.be/abc",
        tmp_path,
        provider="youtube",
        stage_cb=lambda message, percent: stages.append((message, percent)),
    )

    # The same info dict is reused for the download (the site is not asked twice).
    assert instances[-1].processed is instances[0].info
    assert result.path == tmp_path / "input.mp4"
    assert result.size_bytes == 1024
    assert result.extension == ".mp4"
    assert result.provider == "youtube"

    messages = [message for message, _percent in stages]
    assert "Fetching video info" in messages
    assert any("Downloading 50%" in m and "3.2 MB/s" in m for m in messages)
    assert "Merging" in messages
    assert messages[-1] == "Checking file"
    percents = [percent for _message, percent in stages]
    assert percents == sorted(percents), "progress must never jump backwards"


def test_download_with_ytdlp_reports_plain_progress_when_no_stage_callback(monkeypatch, tmp_path):
    progress = []

    class FakeYoutubeDL(_FakeYoutubeDLBase):
        def process_ie_result(self, info, download=False):
            hook = self.options["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": 512, "total_bytes": 1024})
            hook({"status": "finished", "downloaded_bytes": 1024, "total_bytes": 1024})
            Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"x" * 1024)

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    download_with_ytdlp(
        "https://youtu.be/abc",
        tmp_path,
        provider="youtube",
        progress_cb=lambda done, total: progress.append((done, total)),
    )

    assert progress and progress[-1] == (1024, 1024)


def test_download_with_ytdlp_turns_errors_into_friendly_message(monkeypatch, tmp_path):
    class FailingYoutubeDL(_FakeYoutubeDLBase):
        def process_ie_result(self, info, download=False):
            raise downloader.YtDlpDownloadError(
                "ERROR: Private video. Sign in if you've been granted access"
            )

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FailingYoutubeDL)

    with pytest.raises(LinkDownloadError) as err:
        download_with_ytdlp("https://youtu.be/abc", tmp_path, provider="youtube")

    assert "private" in str(err.value).lower()


def test_download_with_ytdlp_stops_when_download_is_too_large(monkeypatch, tmp_path):
    class HugeYoutubeDL(_FakeYoutubeDLBase):
        def process_ie_result(self, info, download=False):
            hook = self.options["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": MAX_FILE_SIZE_BYTES + 1,
                  "total_bytes": MAX_FILE_SIZE_BYTES + 1})

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", HugeYoutubeDL)

    with pytest.raises(LinkTooLargeError) as err:
        download_with_ytdlp("https://youtu.be/abc", tmp_path, provider="youtube")

    assert "limit" in str(err.value).lower()


def test_download_with_ytdlp_gives_up_at_the_deadline_and_cleans_up(monkeypatch, tmp_path):
    """A site that never answers must not block the import thread forever."""
    class HangingYoutubeDL(_FakeYoutubeDLBase):
        def process_ie_result(self, info, download=False):
            time.sleep(0.5)  # longer than the deadline below

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", HangingYoutubeDL)

    partial = tmp_path / "input.mp4.part"
    partial.write_bytes(b"x")

    with pytest.raises(LinkDownloadError) as err:
        download_with_ytdlp(
            "https://youtu.be/abc",
            tmp_path,
            provider="youtube",
            timeout_seconds=0.05,
        )

    assert "too long" in str(err.value).lower()
    assert not partial.exists(), "partial files must be deleted after a timeout"


def test_download_with_ytdlp_rejects_a_too_long_video_before_downloading(monkeypatch, tmp_path):
    """Metadata says the video is over the limit: refuse without downloading."""
    processed = []

    class LongYoutubeDL(_FakeYoutubeDLBase):
        def extract_info(self, url, download=False):
            return {"duration": (MAX_VIDEO_MINUTES + 1) * 60}

        def process_ie_result(self, info, download=False):
            processed.append(info)

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", LongYoutubeDL)

    with pytest.raises(LinkError) as err:
        download_with_ytdlp("https://youtu.be/abc", tmp_path, provider="youtube")

    assert "minutes" in str(err.value).lower()
    assert "limit" in str(err.value).lower()
    assert processed == [], "a too-long video must not be downloaded"


def test_download_with_ytdlp_rejects_a_too_big_video_before_downloading(monkeypatch, tmp_path):
    """Metadata says the file is over the size limit: refuse with a clear message."""
    processed = []

    class BigYoutubeDL(_FakeYoutubeDLBase):
        def extract_info(self, url, download=False):
            return {"duration": 60, "filesize_approx": MAX_FILE_SIZE_BYTES + 1}

        def process_ie_result(self, info, download=False):
            processed.append(info)

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", BigYoutubeDL)

    with pytest.raises(LinkTooLargeError) as err:
        download_with_ytdlp("https://youtu.be/abc", tmp_path, provider="youtube")

    assert "about" in str(err.value).lower()
    assert "limit" in str(err.value).lower()
    assert processed == [], "a too-big video must not be downloaded"


def test_download_with_ytdlp_rejects_a_playlist_found_during_lookup(monkeypatch, tmp_path):
    """A video URL that turns into a playlist is refused from the same info."""
    processed = []

    class PlaylistYoutubeDL(_FakeYoutubeDLBase):
        def extract_info(self, url, download=False):
            return {"_type": "playlist", "entries": [{"id": "a"}, {"id": "b"}]}

        def process_ie_result(self, info, download=False):
            processed.append(info)

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", PlaylistYoutubeDL)

    with pytest.raises(UnsupportedLinkError) as err:
        download_with_ytdlp("https://youtu.be/abc", tmp_path, provider="youtube")

    assert "playlist" in str(err.value).lower()
    assert processed == [], "a playlist must not be downloaded"


def test_download_with_ytdlp_estimates_size_from_formats(monkeypatch, tmp_path):
    """When filesize_approx is missing, estimate from the best mp4 video + m4a audio."""
    class FormatsYoutubeDL(_FakeYoutubeDLBase):
        def extract_info(self, url, download=False):
            return {
                "duration": 60,
                "formats": [
                    {"format_id": "137", "vcodec": "avc1", "acodec": "none",
                     "height": 1080, "filesize": 500},
                    {"format_id": "136", "vcodec": "avc1", "acodec": "none",
                     "height": 720, "filesize": 300},
                    {"format_id": "140", "vcodec": "none", "acodec": "mp4a",
                     "filesize": 100},
                ],
            }

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FormatsYoutubeDL)
    info = downloader.fetch_stream_info("https://youtu.be/abc", tmp_path)

    # 720p cap: 300 (video) + 100 (audio), not the 1080p 500.
    assert downloader.estimate_stream_size(info) == 400


# ---------------------------------------------------------------------------
# Background import job: limits and hand-over to the pipeline
# ---------------------------------------------------------------------------

def test_link_job_starts_in_downloading_state():
    """The UI must never flash 'uploaded' before the download has even started."""
    job = jobs.create_job("clip.mp4", 0, source_url="https://example.com/clip.mp4")
    try:
        assert job["status"] == "downloading"
        assert job["progress_percent"] == 0
        assert "download" in job["progress_message"].lower()
        assert job["source"] == "link"
    finally:
        shutil.rmtree(jobs.get_job_dir(job["job_id"]), ignore_errors=True)


def test_upload_job_still_starts_uploaded():
    job = jobs.create_job("clip.mp4", 1234)
    try:
        assert job["status"] == "uploaded"
        assert job["source"] == "upload"
    finally:
        shutil.rmtree(jobs.get_job_dir(job["job_id"]), ignore_errors=True)


def test_import_job_rejects_video_over_duration_limit(monkeypatch):
    job = jobs.create_job("link.mp4", 0, "blurred_background", source_url="https://example.com/v.mp4")
    job_id = job["job_id"]
    job_dir = jobs.get_job_dir(job_id)

    fake_path = job_dir / "input.mp4"
    fake_path.write_bytes(b"x" * 32)

    monkeypatch.setattr(
        jobs, "download_from_link",
        lambda *args, **kwargs: DownloadResult(
            fake_path, 32, "https://www.youtube.com/watch?v=abc", "video/mp4", ".mp4", provider="youtube"
        ),
    )
    monkeypatch.setattr(
        jobs, "probe_video_file",
        lambda path: {"duration": (MAX_VIDEO_MINUTES + 5) * 60, "width": 640, "height": 360, "codec": "h264"},
    )
    queued = []
    monkeypatch.setattr(jobs, "enqueue_job", lambda jid: queued.append(jid))

    try:
        jobs._run_link_import(job_id, "https://www.youtube.com/watch?v=abc")

        data = jobs.load_job(job_id)
        assert data["status"] == "error"
        assert "limit" in data["error"].lower()
        assert queued == [], "a too-long video must not be queued for processing"
        assert not fake_path.exists(), "the too-long download should be deleted"
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def test_failed_link_import_marks_the_job_error_without_raising(monkeypatch):
    """A timed-out import must finish, so the worker can pick up the next job."""
    job = jobs.create_job("link.mp4", 0, source_url="https://youtu.be/abc")
    job_id = job["job_id"]
    job_dir = jobs.get_job_dir(job_id)

    def fake_download(*args, **kwargs):
        raise LinkDownloadError("The download took too long and was stopped.")

    monkeypatch.setattr(jobs, "download_from_link", fake_download)
    queued = []
    monkeypatch.setattr(jobs, "enqueue_job", lambda jid: queued.append(jid))

    try:
        jobs._run_link_import(job_id, "https://youtu.be/abc")  # must not raise

        data = jobs.load_job(job_id)
        assert data["status"] == "error"
        assert "too long" in data["error"].lower()
        assert queued == []
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def test_import_job_hands_valid_video_to_pipeline(monkeypatch):
    job = jobs.create_job("link.mp4", 0, "blurred_background", source_url="https://example.com/v.mp4")
    job_id = job["job_id"]
    job_dir = jobs.get_job_dir(job_id)

    fake_path = job_dir / "input.mp4"
    fake_path.write_bytes(b"x" * 64)

    monkeypatch.setattr(
        jobs, "download_from_link",
        lambda *args, **kwargs: DownloadResult(
            fake_path, 64, "https://example.com/v.mp4", "video/mp4", ".mp4", provider="direct"
        ),
    )
    monkeypatch.setattr(
        jobs, "probe_video_file",
        lambda path: {"duration": 95.5, "width": 1920, "height": 1080, "codec": "h264"},
    )
    queued = []
    monkeypatch.setattr(jobs, "enqueue_job", lambda jid: queued.append(jid))

    try:
        jobs._run_link_import(job_id, "https://example.com/v.mp4")

        data = jobs.load_job(job_id)
        assert queued == [job_id]
        assert data["status"] == "uploaded"
        assert data["source"] == "link"
        assert data["import_provider"] == "direct"
        assert data["duration_seconds"] == 95.5
        assert data["video_width"] == 1920
        assert data["file_size_bytes"] == 64
        assert data["error"] is None
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# HTTP API and page
# ---------------------------------------------------------------------------

def test_home_page_offers_import_from_link_and_shows_note(monkeypatch):
    monkeypatch.setattr(app_module, "ENABLE_YOUTUBE_IMPORT", False)
    response = client.get("/")
    assert response.status_code == 200
    assert "Import from link" in response.text
    assert "Only upload content you own or have permission to clip." in response.text
    # Streaming sites are off by default, so the page must not advertise them.
    assert "YouTube" not in response.text
    assert "turned off on this copy" in response.text


def test_import_endpoint_rejects_unsupported_streaming_site():
    """Sites yt-dlp is not wired up for must still be refused with a clear message."""
    response = client.post("/api/import-link", json={"url": "https://vimeo.com/123456789"})
    assert response.status_code == 400
    assert "not supported" in response.json()["detail"].lower()


def test_import_endpoint_accepts_youtube_link(monkeypatch):
    """A YouTube link starts a job straight away; the download runs in the background."""
    monkeypatch.setattr(app_module, "ENABLE_YOUTUBE_IMPORT", True)
    # No real DNS lookup, runtime check or download during tests.
    monkeypatch.setattr(app_module, "validate_public_url", lambda url: None)
    monkeypatch.setattr(app_module, "check_ytdlp_runtime", lambda: None)
    started = []
    monkeypatch.setattr(
        app_module, "start_link_import", lambda job_id, url: started.append((job_id, url))
    )

    before = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}
    response = client.post(
        "/api/import-link", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    )
    after = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}

    try:
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["provider"] == "youtube"
        assert body["status"] == "downloading"
        assert started and started[0][0] == body["job_id"]

        # The job must start in "downloading", never "uploaded".
        data = jobs.load_job(body["job_id"])
        assert data["status"] == "downloading"
        assert data["source"] == "link"
        assert data["original_filename"] == "YouTube video"
    finally:
        new_jobs = after - before
        for name in new_jobs:
            shutil.rmtree(JOBS_DIR / name, ignore_errors=True)


def test_import_endpoint_rejects_streaming_site_when_disabled(monkeypatch):
    """The public default is OFF: no job is created and the message is friendly."""
    monkeypatch.setattr(app_module, "ENABLE_YOUTUBE_IMPORT", False)
    before = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}
    response = client.post(
        "/api/import-link", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    )
    after = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}

    assert response.status_code == 400
    assert "turned off on this copy" in response.json()["detail"]
    assert before == after, "a disabled import must not create a job"


def test_import_endpoint_shows_runtime_install_hint(monkeypatch):
    """Missing Node 22+ / yt_dlp_ejs must be explained before a job is created."""
    monkeypatch.setattr(app_module, "ENABLE_YOUTUBE_IMPORT", True)
    monkeypatch.setattr(app_module, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        app_module,
        "check_ytdlp_runtime",
        lambda: 'Link import is missing yt_dlp_ejs. Run: pip install -U "yt-dlp[default]"',
    )

    before = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}
    response = client.post(
        "/api/import-link", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    )
    after = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}

    assert response.status_code == 400
    assert 'pip install -U "yt-dlp[default]"' in response.json()["detail"]
    assert before == after, "a blocked import must not create a job"


def test_import_endpoint_rejects_private_address_without_creating_a_job():
    before = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}
    response = client.post("/api/import-link", json={"url": "http://127.0.0.1/video.mp4"})
    after = {p.name for p in JOBS_DIR.iterdir() if p.is_dir()}

    assert response.status_code == 400
    assert "private" in response.json()["detail"].lower()
    assert before == after, "a blocked link must not create a job"


def test_import_endpoint_rejects_empty_url():
    response = client.post("/api/import-link", json={"url": "   "})
    assert response.status_code == 400
    assert "paste a link" in response.json()["detail"].lower()
