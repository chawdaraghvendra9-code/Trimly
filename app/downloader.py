"""
Import a video from a link ("Import from link" on the upload page).

Supported:
  * direct video file links ending in .mp4 / .mov / .webm / .mkv
  * public Google Drive share links  (drive.google.com/file/d/<id>/view)
  * public Dropbox share links       (dropbox.com/s/... or dropbox.com/scl/fi/...)
  * YouTube, TikTok and Instagram video pages (downloaded with yt-dlp)

NOT supported: other streaming sites (Vimeo, Twitch, Facebook, ...), live
streams, playlists and paywalled/members-only videos.

Safety: for plain file links, every request (including every redirect hop) has
its hostname resolved first, and the download is refused if it points at this
computer or a private network. Streaming sites are fetched by yt-dlp, which only
accepts the domains listed below.
"""
import importlib.util
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

import requests
import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from app.config import (
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    MAX_VIDEO_MINUTES,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
    YTDLP_MAX_HEIGHT,
    YTDLP_TIMEOUT_SECONDS,
)
from app.logger import logger
from app.video import find_ffmpeg_executable

# Video files we accept when importing from a link.
IMPORT_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}

# Servers often send a generic type for a direct file. These are fine;
# ffprobe decides at the end whether the file really is a video.
GENERIC_CONTENT_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
    "application/force-download",
    "application/download",
}

# Used when a link has no file extension, e.g. Dropbox or a signed CDN URL.
CONTENT_TYPE_TO_EXT = {
    "video/mp4": ".mp4",
    "video/x-m4v": ".mp4",
    "application/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "application/x-matroska": ".mkv",
}

# Sites we hand to yt-dlp (it has a supported extractor for each of these).
# These are the only streaming sites we support - anything else is refused.
YTDLP_HOSTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "youtube-nocookie.com": "youtube",
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
}

# Sites that look like streaming sites but are NOT supported, so we can say so.
UNSUPPORTED_STREAMING_HOSTS = {
    "vimeo.com", "dailymotion.com", "dai.ly", "twitch.tv", "clips.twitch.tv",
    "soundcloud.com", "bilibili.com", "facebook.com", "fb.watch", "fb.com",
    "twitter.com", "x.com",
}

GOOGLE_DRIVE_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
    "drive.googleusercontent.com",
}

DROPBOX_HOSTS = {
    "dropbox.com",
    "www.dropbox.com",
    "dl.dropbox.com",
    "dl.dropboxusercontent.com",
}

# Only standard web ports, to keep link imports boring and safe.
ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
CHUNK_SIZE = 256 * 1024
USER_AGENT = "CliptorClipStudio/1.0 (local app; direct file downloads only)"

# Matches the file id inside Drive links like /file/d/<id>/view
_DRIVE_ID_PATTERN = re.compile(r"/(?:file/)?d/([A-Za-z0-9_-]{10,})")


class LinkError(Exception):
    """Base class for friendly 'import from link' problems."""


class UnsupportedLinkError(LinkError):
    """The link is not a supported kind of video link."""


class UnsafeLinkError(LinkError):
    """The link points at this machine or a private network."""


class LinkTooLargeError(LinkError):
    """The remote file is bigger than the configured limit."""


class LinkDownloadError(LinkError):
    """Something went wrong while downloading."""


@dataclass
class DownloadResult:
    """Where the downloaded file landed, plus a few facts about it."""
    path: Path
    size_bytes: int
    final_url: str
    content_type: str
    extension: str
    provider: str = "direct"


# ---------------------------------------------------------------------------
# Reading the link
# ---------------------------------------------------------------------------

def _split_url(url: str) -> Tuple[Any, str, int]:
    """Parses a URL and returns (parsed, hostname, port). Raises a friendly error."""
    text = (url or "").strip()
    if not text:
        raise UnsupportedLinkError("Please paste a link first.")
    if len(text) > MAX_URL_LENGTH:
        raise UnsupportedLinkError("That link is too long to be a normal video link.")
    try:
        parsed = urlparse(text)
    except ValueError:
        raise UnsupportedLinkError("That link is not a valid web address.") from None
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsupportedLinkError("Only http:// and https:// links are supported.")

    try:
        # parsed.hostname handles IPv6 like [::1] and strips any user:pass part.
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise UnsupportedLinkError("That link is not a valid web address.") from None

    if not host:
        raise UnsupportedLinkError("That link has no website address in it.")
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed, host, port


def _host_in(host: str, hosts: set) -> bool:
    """True if host is in the set or is a subdomain of something in the set."""
    return any(host == item or host.endswith("." + item) for item in hosts)


def _ytdlp_provider_for_host(host: str) -> Optional[str]:
    """Returns 'youtube', 'tiktok' or 'instagram' when yt-dlp handles this host."""
    for domain, provider in YTDLP_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return provider
    return None


def _is_playlist_link(parsed: Any, provider: str) -> bool:
    """True when the link points at a whole playlist instead of one video."""
    if provider != "youtube":
        return False
    if "/playlist" in (parsed.path or "").lower():
        return True
    query = parse_qs(parsed.query)
    # watch?v=...&list=... is still one video, so v= wins.
    return "list" in query and "v" not in query


def drive_file_id(url: str) -> Optional[str]:
    """Finds the Google Drive file id in a share link, or returns None."""
    parsed, host, _port = _split_url(url)
    if not _host_in(host, GOOGLE_DRIVE_HOSTS):
        return None

    query = parse_qs(parsed.query)
    if query.get("id"):
        return query["id"][0]

    match = _DRIVE_ID_PATTERN.search(parsed.path)
    if match:
        return match.group(1)
    return None


def build_drive_direct_url(url: str) -> Optional[str]:
    """Turns a Google Drive share link into a direct download URL."""
    file_id = drive_file_id(url)
    if not file_id:
        return None
    # usercontent.google.com is the endpoint that still serves the file itself.
    return (
        "https://drive.usercontent.google.com/download"
        f"?id={quote(file_id)}&export=download&confirm=t"
    )


def build_dropbox_direct_url(url: str) -> Optional[str]:
    """Turns a Dropbox share link into a direct download URL (dl=1)."""
    parsed, host, _port = _split_url(url)
    if not _host_in(host, DROPBOX_HOSTS):
        return None
    if host.endswith("dropboxusercontent.com"):
        return url  # already a direct file link

    # Keep every existing parameter (for example rlkey) and just switch dl=1.
    query = parsed.query
    if re.search(r"(^|&)dl=[^&]*", query):
        query = re.sub(r"(^|&)dl=[^&]*", r"\1dl=1", query)
    else:
        query = f"{query}&dl=1" if query else "dl=1"
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))


def convert_share_link(url: str) -> Tuple[str, str]:
    """
    Returns (url_to_download, provider).

    provider is one of:
      "google_drive", "dropbox", "youtube", "tiktok", "instagram", "direct".

    For streaming sites the returned URL is still the page URL, because yt-dlp
    needs the page (not a guessed file URL) to find the video.
    """
    text = (url or "").strip()
    parsed, host, _port = _split_url(text)

    streaming_provider = _ytdlp_provider_for_host(host)
    if streaming_provider:
        # yt-dlp would download every video in a playlist into the same file.
        if _is_playlist_link(parsed, streaming_provider):
            raise UnsupportedLinkError(
                "That link is a playlist. Paste a link to a single video."
            )
        return text, streaming_provider

    if _host_in(host, UNSUPPORTED_STREAMING_HOSTS):
        raise UnsupportedLinkError(
            "That streaming site is not supported. You can use YouTube, TikTok or "
            "Instagram links, a direct video file link, or a Google Drive / Dropbox "
            "share link."
        )

    if _host_in(host, GOOGLE_DRIVE_HOSTS):
        direct = build_drive_direct_url(text)
        if not direct:
            raise UnsupportedLinkError(
                "That Google Drive link does not contain a file id. "
                "Open the file in Drive, click Share, and copy the link."
            )
        return direct, "google_drive"

    dropbox = build_dropbox_direct_url(text)
    if dropbox:
        return dropbox, "dropbox"

    # A plain direct link: reject obvious non-video files early with a clear message.
    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix and suffix not in IMPORT_EXTENSIONS:
        raise UnsupportedLinkError(
            f"That link points to a '{suffix}' file. Supported video formats: "
            "MP4, MOV, WEBM, MKV."
        )
    return text, "direct"


# ---------------------------------------------------------------------------
# Blocking private / local addresses (SSRF protection)
# ---------------------------------------------------------------------------

def is_public_ip(ip: str) -> bool:
    """True only for normal public internet addresses."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private          # 10/8, 172.16/12, 192.168/16, fc00::/7, ...
        or addr.is_loopback      # 127/8, ::1
        or addr.is_link_local    # 169.254/16, fe80::/10
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified   # 0.0.0.0, ::
    )


def resolve_host_ips(host: str) -> List[str]:
    """Resolves a hostname to every address it points at (IPv4 and IPv6)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise LinkDownloadError(
            f"Could not find the website '{host}'. Check the link and your internet connection."
        ) from e

    ips: List[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise LinkDownloadError(f"Could not find any address for '{host}'.")
    return ips


def validate_public_url(
    url: str,
    resolver: Callable[[str], List[str]] = resolve_host_ips,
) -> None:
    """
    Refuses links that point at this computer or a private network.
    Runs again for every redirect hop, so a public link cannot bounce to 127.0.0.1.
    """
    _parsed, host, port = _split_url(url)
    if port not in ALLOWED_PORTS:
        raise UnsafeLinkError(
            f"Only the standard web ports 80 and 443 are allowed, but this link uses port {port}."
        )
    for ip in resolver(host):
        if not is_public_ip(ip):
            raise UnsafeLinkError(
                "That link points to a private or local network address, which is not allowed."
            )


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def pick_extension(url: str, content_type: str = "", content_disposition: str = "") -> Optional[str]:
    """Works out the video file extension from the URL, headers, or content type."""
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix in IMPORT_EXTENSIONS:
        return suffix

    if content_disposition:
        match = re.search(
            r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", content_disposition, re.IGNORECASE
        )
        if match:
            name_suffix = Path(unquote(match.group(1).strip())).suffix.lower()
            if name_suffix in IMPORT_EXTENSIONS:
                return name_suffix

    base_type = (content_type or "").split(";")[0].strip().lower()
    return CONTENT_TYPE_TO_EXT.get(base_type)


def _open_with_safe_redirects(
    session: requests.Session,
    url: str,
    validate_url: Callable[[str], None],
) -> Tuple[str, requests.Response]:
    """Follows redirects by hand so every hop can be checked before it is fetched."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        validate_url(current)
        try:
            response = session.get(
                current,
                stream=True,
                allow_redirects=False,
                timeout=(15, 60),
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as e:
            raise LinkDownloadError(
                f"Could not reach the link ({e.__class__.__name__}). "
                "Check your internet connection and try again."
            ) from e

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location") or ""
            response.close()
            if not location:
                raise LinkDownloadError("The link redirected nowhere.")
            current = urljoin(current, location)
            continue
        return current, response

    raise LinkDownloadError("The link redirected too many times.")


def _mb_text(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def download_video(
    url: str,
    dest_dir: Path,
    filename_stem: str = "input",
    progress_cb: Optional[Callable[[int, int], None]] = None,
    validate_url: Callable[[str], None] = validate_public_url,
    max_bytes: int = MAX_FILE_SIZE_BYTES,
    timeout_seconds: float = 60 * 30,
    session: Optional[requests.Session] = None,
) -> DownloadResult:
    """
    Downloads a direct video URL into dest_dir and returns where it landed.
    progress_cb(downloaded_bytes, total_bytes_or_0) is called while it streams.
    """
    owns_session = session is None
    session = session or requests.Session()
    final_url, response = _open_with_safe_redirects(session, url, validate_url)
    temp_path: Optional[Path] = None
    started = time.monotonic()

    try:
        status = response.status_code
        if status == 404:
            raise LinkDownloadError(
                "The file was not found (404). If this is a Google Drive or Dropbox link, "
                "make sure the file still exists and is shared with 'Anyone with the link'."
            )
        if status in (401, 403):
            raise LinkDownloadError(
                "The server refused the download, so the file is probably private. "
                "For Google Drive or Dropbox, set sharing to 'Anyone with the link' and try again."
            )
        if status >= 400:
            raise LinkDownloadError(f"The server answered with an error (HTTP {status}).")

        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        content_disposition = response.headers.get("Content-Disposition") or ""

        # A share link that is not public answers with a web page instead of the file.
        if content_type.startswith("text/") or content_type in {"application/xhtml+xml", "application/json"}:
            raise LinkDownloadError(
                "The link returned a web page instead of a video file. Make sure the file is "
                "shared with 'Anyone with the link'. Large Google Drive files cannot be "
                "downloaded this way - please download the file and upload it instead."
            )

        if (
            content_type
            and not content_type.startswith("video/")
            and content_type not in GENERIC_CONTENT_TYPES
        ):
            raise UnsupportedLinkError(
                f"That link returned '{content_type}' instead of a video file. "
                "Supported formats: MP4, MOV, WEBM, MKV."
            )

        extension = pick_extension(final_url, content_type, content_disposition)
        if not extension:
            raise UnsupportedLinkError(
                "That link does not look like a video file. Supported formats: MP4, MOV, WEBM, MKV."
            )

        total_bytes = 0
        try:
            total_bytes = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total_bytes = 0
        if total_bytes > max_bytes:
            raise LinkTooLargeError(
                f"This file is {_mb_text(total_bytes)}, which is over the "
                f"{MAX_FILE_SIZE_MB} MB limit."
            )

        temp_path = dest_dir / f"{filename_stem}{extension}.part"
        final_path = dest_dir / f"{filename_stem}{extension}"
        written = 0
        last_report = 0.0

        with open(temp_path, "wb") as handle:
            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise LinkTooLargeError(
                        f"The download went over the {MAX_FILE_SIZE_MB} MB limit and was stopped."
                    )
                handle.write(chunk)

                if time.monotonic() - started > timeout_seconds:
                    raise LinkDownloadError("The download took too long and was stopped.")

                # Do not hammer job.json - about three updates per second at most.
                now = time.monotonic()
                if progress_cb and (now - last_report >= 0.4 or (total_bytes and written >= total_bytes)):
                    last_report = now
                    progress_cb(written, total_bytes)

        if written == 0:
            raise LinkDownloadError("The download was empty (0 bytes).")

        os.replace(temp_path, final_path)
        temp_path = None
        logger.info(f"Imported link file: {final_path.name} ({_mb_text(written)}, {content_type or 'unknown type'})")

        return DownloadResult(
            path=final_path,
            size_bytes=written,
            final_url=final_url,
            content_type=content_type,
            extension=extension,
        )

    except requests.RequestException as e:
        raise LinkDownloadError(
            "The connection dropped while downloading. Please try again."
        ) from e
    finally:
        response.close()
        if owns_session:
            session.close()
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logger.warning(f"Could not delete partial download {temp_path}")


# ---------------------------------------------------------------------------
# Streaming sites (YouTube, TikTok, Instagram) via yt-dlp
# ---------------------------------------------------------------------------

def _cookies_from_browser(value: str) -> Optional[Tuple[str, ...]]:
    """'chrome' -> ('chrome',); 'chrome:Profile 1' -> ('chrome', 'Profile 1')."""
    if not value:
        return None
    parts = [part.strip() for part in value.split(":") if part.strip()]
    return tuple(parts) if parts else None


# Shown whenever link imports are missing a runtime they need.
YTDLP_RUNTIME_HELP = 'Run: pip install -U "yt-dlp[default]"'


def _node_major_version(node_path: str) -> Optional[int]:
    """Returns the major Node.js version from 'node --version', or None."""
    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"v?(\d+)\.", result.stdout.strip())
    return int(match.group(1)) if match else None


def check_ytdlp_runtime() -> Optional[str]:
    """
    Returns None when link imports are ready to go, or a clear message when not.
    Needs Node.js 22+ (for yt-dlp's JavaScript challenges) and yt_dlp_ejs.
    """
    node = shutil.which("node")
    if not node:
        return (
            "Link import needs Node.js 22 or newer, but Node was not found. "
            f"Install Node.js 22+, then {YTDLP_RUNTIME_HELP}"
        )

    major = _node_major_version(node)
    if major is None or major < 22:
        found = f"v{major}" if major else "an unknown version"
        return (
            f"Link import needs Node.js 22 or newer, but Node {found} was found. "
            f"Update Node.js, then {YTDLP_RUNTIME_HELP}"
        )

    if importlib.util.find_spec("yt_dlp_ejs") is None:
        return (
            "Link import is missing yt_dlp_ejs, the JavaScript helper for yt-dlp. "
            f"{YTDLP_RUNTIME_HELP}"
        )

    return None


def build_ytdlp_options(
    dest_dir: Path,
    progress_hook: Optional[Callable] = None,
    postprocessor_hook: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Options for yt-dlp: mp4 (avc1 + m4a) up to YTDLP_MAX_HEIGHT, merged into
    one MP4, one video only, and 4 parallel fragments for speed.
    """
    options: Dict[str, Any] = {
        "outtmpl": str(dest_dir / "input.%(ext)s"),
        "format": (
            f"bestvideo[height<={YTDLP_MAX_HEIGHT}][vcodec^=avc1]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={YTDLP_MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={YTDLP_MAX_HEIGHT}]+bestaudio"
            f"/best[height<={YTDLP_MAX_HEIGHT}]/best"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "overwrites": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 15,
        "continuedl": False,
    }
    if progress_hook:
        options["progress_hooks"] = [progress_hook]
    if postprocessor_hook:
        options["postprocessor_hooks"] = [postprocessor_hook]
    if YTDLP_COOKIES_FILE:
        options["cookiefile"] = YTDLP_COOKIES_FILE
    browser_cookies = _cookies_from_browser(YTDLP_COOKIES_FROM_BROWSER)
    if browser_cookies:
        options["cookiesfrombrowser"] = browser_cookies

    # Tell yt-dlp where ffmpeg is, so merging works even when PATH changes.
    ffmpeg = find_ffmpeg_executable()
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg

    # YouTube needs a JavaScript runtime to solve its signature challenges.
    # Only enable Node when it is really installed on this computer.
    node = shutil.which("node")
    if node:
        options["js_runtimes"] = {"node": {"path": node}}

    return options


def friendly_ytdlp_error(message: str) -> str:
    """Turns yt-dlp's long error text into one sentence a beginner can act on."""
    text = (message or "").lower()

    if (
        "challenge" in text
        or "nsig" in text
        or "failed to solve" in text
        or "solving failed" in text
        or "js runtime" in text
    ):
        return (
            "The site's safety check could not be solved. Install Node.js 22+ and run: "
            'pip install -U "yt-dlp[default]", then try again.'
        )
    if "sign in to confirm" in text or "not a bot" in text or "confirm you" in text:
        return (
            "The site asked this computer to prove it is not a bot. Try again in a few "
            'minutes. If it keeps happening, install Node.js 22+ and run: pip install -U "yt-dlp[default]".'
        )
    if "private video" in text or "members-only" in text or "login required" in text or "requires payment" in text:
        return "That video is private or paid, so it cannot be downloaded."
    if "is a playlist" in text or "playlist does not exist" in text:
        return "That link is a playlist. Paste a link to a single video."
    if "live stream" in text or "premieres in" in text or "is live" in text:
        return "Live streams and upcoming premieres cannot be imported."
    if "http error 429" in text or "too many requests" in text:
        return "The site is rate-limiting this computer. Wait a few minutes and try again."
    if "unsupported url" in text:
        return "That link is not a video page we can download."
    if "not available in your country" in text or "region" in text or "geo" in text:
        return "That video is region-locked and cannot be imported from your country."
    if "requested format is not available" in text:
        return (
            "That video has no downloadable format we can use at this quality. "
            "Try setting YTDLP_MAX_HEIGHT=480 in .env and import again."
        )
    if (
        "video unavailable" in text
        or "has been removed" in text
        or "unavailable" in text
    ):
        return "That video is unavailable (removed or not public)."
    return "That video could not be downloaded from the link. Please check the link and try again."


def _newest_downloaded_file(dest_dir: Path) -> Optional[Path]:
    """Finds the file yt-dlp just wrote, ignoring its temporary files."""
    candidates = [
        item
        for item in dest_dir.glob("input*")
        if item.is_file() and not item.name.endswith((".part", ".ytdl", ".temp"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _run_with_deadline(
    work: Callable[[], None],
    timeout_seconds: float,
    timeout_message: str,
) -> None:
    """
    Runs work() in a background thread and gives up after timeout_seconds.
    yt-dlp sometimes hangs while reading a web page; without this the import
    thread (and the job queue behind it) would wait forever.
    """
    outcome: Dict[str, BaseException] = {}

    def runner() -> None:
        try:
            work()
        except BaseException as e:  # re-raised on the calling thread below
            outcome["error"] = e

    thread = threading.Thread(target=runner, daemon=True, name="ytdlp-work")
    thread.start()
    thread.join(timeout_seconds)

    if thread.is_alive():
        raise LinkDownloadError(timeout_message)

    error = outcome.get("error")
    if error is not None:
        raise error


def fetch_stream_info(
    url: str,
    dest_dir: Path,
    timeout_seconds: float = 120,
) -> Dict[str, Any]:
    """
    Asks yt-dlp for the video's details without downloading anything.
    Lets us refuse a too-long or too-big video before the download starts.
    """
    options = build_ytdlp_options(dest_dir)
    options["skip_download"] = True
    holder: Dict[str, Any] = {}

    def work() -> None:
        with yt_dlp.YoutubeDL(options) as ydl:
            holder["info"] = ydl.extract_info(url, download=False) or {}

    try:
        _run_with_deadline(
            work,
            timeout_seconds,
            "Looking up the video took too long and was stopped. Please try again.",
        )
    except LinkError:
        raise
    except YtDlpDownloadError as e:
        raise LinkDownloadError(friendly_ytdlp_error(str(e))) from e
    except Exception as e:
        # Never leak yt-dlp's long messages to the UI.
        raise LinkDownloadError(friendly_ytdlp_error(str(e))) from e

    return holder.get("info") or {}


def _check_stream_info(info: Dict[str, Any], max_bytes: int) -> None:
    """Refuses playlists, too-long and too-big videos using the info we already have."""
    if info.get("_type") == "playlist" or info.get("entries"):
        raise UnsupportedLinkError(
            "That link is a playlist. Paste a link to a single video."
        )

    duration = float(info.get("duration") or 0)
    if duration > MAX_VIDEO_MINUTES * 60:
        raise LinkError(
            f"This video is {duration / 60:.1f} minutes long. The limit is "
            f"{MAX_VIDEO_MINUTES} minutes, so please trim it and import again."
        )

    estimated_size = estimate_stream_size(info)
    if estimated_size > max_bytes:
        raise LinkTooLargeError(
            f"This video is about {_mb_text(estimated_size)}, which is over the "
            f"{MAX_FILE_SIZE_MB} MB limit."
        )


def estimate_stream_size(info: Dict[str, Any]) -> int:
    """Best-effort download size from the info we already fetched (0 = unknown)."""
    direct = int(info.get("filesize_approx") or info.get("filesize") or 0)
    if direct:
        return direct

    sizes = []
    for fmt in info.get("formats") or []:
        size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
        if size:
            sizes.append((fmt, size))
    if not sizes:
        return 0

    def biggest(matches: Callable[[Dict[str, Any]], bool]) -> int:
        found = [size for fmt, size in sizes if matches(fmt)]
        return max(found) if found else 0

    video = biggest(
        lambda fmt: fmt.get("vcodec") not in (None, "none")
        and (fmt.get("height") or 0) <= YTDLP_MAX_HEIGHT
    )
    audio = biggest(
        lambda fmt: fmt.get("vcodec") in (None, "none")
        and fmt.get("acodec") not in (None, "none")
    )
    return video + audio


def download_with_ytdlp(
    url: str,
    dest_dir: Path,
    provider: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stage_cb: Optional[Callable[[str, int], None]] = None,
    max_bytes: int = MAX_FILE_SIZE_BYTES,
    timeout_seconds: float = YTDLP_TIMEOUT_SECONDS,
) -> DownloadResult:
    """
    Downloads a YouTube / TikTok / Instagram video into dest_dir.
    The page is fetched once; the same info is then used to download.
    """
    last_report = 0.0
    last_percent = 0
    last_stage_percent = 0
    merge_started: Optional[float] = None
    timeout_message = (
        "The download took too long and was stopped. Please try again, "
        "or use a shorter video."
    )

    def report_stage(message: str, percent: int) -> None:
        """Sends the stage to the UI; the percent never goes backwards."""
        nonlocal last_stage_percent
        last_stage_percent = max(last_stage_percent, percent)
        if stage_cb:
            stage_cb(message, last_stage_percent)

    # 1. One lookup: duration, playlist and size are all checked from this.
    report_stage("Fetching video info", 3)
    info_started = time.monotonic()
    info = fetch_stream_info(url, dest_dir, timeout_seconds=min(timeout_seconds, 120))
    logger.info(f"yt-dlp info extraction took {time.monotonic() - info_started:.2f}s")

    _check_stream_info(info, max_bytes)
    report_stage("Starting download", 10)
    download_started = time.monotonic()

    def hook(update: Dict[str, Any]) -> None:
        nonlocal last_report, last_percent
        status = update.get("status")
        if status == "downloading":
            downloaded = int(update.get("downloaded_bytes") or 0)
            total = int(update.get("total_bytes") or update.get("total_bytes_estimate") or 0)
            # Stop early when the site already says the file is too big.
            if downloaded > max_bytes or total > max_bytes:
                # Raising from the hook stops yt-dlp immediately.
                raise LinkTooLargeError(
                    f"This video is bigger than the {MAX_FILE_SIZE_MB} MB limit, so it was stopped."
                )
            # Stop a slow download cleanly, so its files are closed and deleted.
            if time.monotonic() - download_started > timeout_seconds:
                raise LinkDownloadError(timeout_message)

            if total > 0:
                # Audio and video download separately; keep the highest percent.
                last_percent = min(99, max(last_percent, int(downloaded * 100 / total)))

            if stage_cb:
                speed = update.get("speed")
                if not speed:
                    elapsed = max(0.1, time.monotonic() - download_started)
                    speed = downloaded / elapsed
                mb_per_second = (speed or 0) / (1024 * 1024)
                report_stage(
                    f"Downloading {last_percent}% ({mb_per_second:.1f} MB/s)",
                    10 + int(last_percent * 0.8),
                )

            now = time.monotonic()
            # Stage messages already show percent and speed; plain byte progress
            # would fight with them, so only use it when no stage callback is set.
            if progress_cb and stage_cb is None and now - last_report >= 0.5:
                last_report = now
                progress_cb(downloaded, total)
        elif status == "finished" and progress_cb:
            downloaded = int(update.get("total_bytes") or update.get("downloaded_bytes") or 0)
            progress_cb(downloaded, downloaded)

    def pp_hook(update: Dict[str, Any]) -> None:
        nonlocal merge_started
        postprocessor = str(update.get("postprocessor") or "")
        if "merger" not in postprocessor.lower():
            return
        if update.get("status") == "started":
            merge_started = time.monotonic()
            report_stage("Merging", 92)
        elif update.get("status") == "finished" and merge_started is not None:
            logger.info(f"yt-dlp merge took {time.monotonic() - merge_started:.2f}s")

    options = build_ytdlp_options(
        dest_dir,
        progress_hook=hook,
        postprocessor_hook=pp_hook,
    )

    def run_download() -> None:
        # Use the info we already fetched, so the site is not asked twice.
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.process_ie_result(info, download=True)

    try:
        # The thread + deadline also covers hangs before the first progress report.
        _run_with_deadline(run_download, timeout_seconds, timeout_message)
    except LinkTooLargeError:
        cleanup_partial_download(dest_dir)
        raise
    except LinkError:
        cleanup_partial_download(dest_dir)
        raise
    except YtDlpDownloadError as e:
        cleanup_partial_download(dest_dir)
        raise LinkDownloadError(friendly_ytdlp_error(str(e))) from e
    except Exception as e:
        # yt-dlp raises a few other exception types; never leak them to the UI.
        cleanup_partial_download(dest_dir)
        raise LinkDownloadError(friendly_ytdlp_error(str(e))) from e

    logger.info(f"yt-dlp download took {time.monotonic() - download_started:.2f}s")
    report_stage("Checking file", 96)

    path = _newest_downloaded_file(dest_dir)
    if path is None:
        raise LinkDownloadError("The site did not return a video file for that link.")

    size = path.stat().st_size
    if size == 0:
        path.unlink(missing_ok=True)
        raise LinkDownloadError("The download was empty (0 bytes).")
    if size > max_bytes:
        path.unlink(missing_ok=True)
        raise LinkTooLargeError(
            f"This file is {_mb_text(size)}, which is over the {MAX_FILE_SIZE_MB} MB limit."
        )

    extension = path.suffix.lower()
    if extension not in IMPORT_EXTENSIONS:
        path.unlink(missing_ok=True)
        raise UnsupportedLinkError(
            "That link only produced an unsupported file type, so it cannot be clipped."
        )

    content_type = "video/mp4" if extension == ".mp4" else f"video/{extension.lstrip('.')}"
    logger.info(f"Imported {provider} video: {path.name} ({_mb_text(size)})")

    return DownloadResult(
        path=path,
        size_bytes=size,
        final_url=url,
        content_type=content_type,
        extension=extension,
        provider=provider,
    )


def download_from_link(
    url: str,
    dest_dir: Path,
    filename_stem: str = "input",
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stage_cb: Optional[Callable[[str, int], None]] = None,
) -> DownloadResult:
    """
    One entry point for every supported link: picks the right downloader.
    Share links are converted first, then the file is fetched.
    """
    target, provider = convert_share_link(url)

    if provider in {"youtube", "tiktok", "instagram"}:
        return download_with_ytdlp(
            target,
            dest_dir,
            provider=provider,
            progress_cb=progress_cb,
            stage_cb=stage_cb,
        )

    result = download_video(
        target,
        dest_dir,
        filename_stem=filename_stem,
        progress_cb=progress_cb,
    )
    result.provider = provider
    return result


def cleanup_partial_download(job_dir: Path) -> None:
    """
    Removes downloaded files after a failed import, including .part files,
    so a broken download is never picked up by the processing pipeline.
    """
    for leftover in list(job_dir.glob("input.*")):
        try:
            leftover.unlink()
        except OSError:
            logger.warning(f"Could not delete {leftover}")


def friendly_link_error(error: Exception) -> str:
    """Turns an exception into one short sentence we can show in the UI."""
    if isinstance(error, LinkError):
        return str(error)
    return "The video could not be imported from that link. Please check the link and try again."
