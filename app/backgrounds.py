"""
Background footage for the "Split screen" video style.

No footage is bundled: the backgrounds/ folder starts empty and the user drops
in their own clips (or uploads them from the results page). Only mp4, webm and
mov are used, and uploads are checked with ffprobe: 60 seconds and 100 MB at
most, and it must be a real video file.
"""
import logging
import random
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("cliptor")

BACKGROUND_EXTENSIONS = {".mp4", ".webm", ".mov"}
MAX_BACKGROUND_SECONDS = 60.0
MAX_BACKGROUND_BYTES = 100 * 1024 * 1024

BACKGROUNDS_DIR = Path(__file__).resolve().parent.parent / "backgrounds"


def ensure_backgrounds_dir() -> Path:
    BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    return BACKGROUNDS_DIR


def list_backgrounds() -> List[Dict[str, Any]]:
    """The clips sitting in backgrounds/. No probing, so the page stays fast."""
    folder = ensure_backgrounds_dir()
    items: List[Dict[str, Any]] = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.suffix.lower() in BACKGROUND_EXTENSIONS:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            items.append({"name": path.name, "size": size})
    return items


def resolve_background(value: Optional[str]) -> Optional[Path]:
    """
    Turns the saved setting into a file.

    - 'random' picks one of the clips in backgrounds/
    - a plain name is looked up inside backgrounds/ (folders are never used)
    - an absolute path is accepted only when it is inside backgrounds/
    """
    folder = ensure_backgrounds_dir()
    if value in (None, "", "none"):
        return None

    if value == "random":
        names = [item["name"] for item in list_backgrounds()]
        if not names:
            return None
        value = random.choice(names)

    candidate = Path(value)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve()
            candidate.relative_to(folder.resolve())
        except (OSError, ValueError):
            return None
    else:
        candidate = folder / candidate.name

    return candidate if candidate.is_file() else None


def check_background(path: Path) -> Optional[str]:
    """None when the clip can be used, otherwise a message for the user."""
    # Imported here so app.video can import this module without a loop.
    from app.video import InvalidVideoError, probe_video_file

    if not path.exists():
        return "That background file was not found."
    if path.suffix.lower() not in BACKGROUND_EXTENSIONS:
        return "Use an .mp4, .webm or .mov background clip."

    try:
        size = path.stat().st_size
    except OSError:
        return "That background file could not be read."
    if size > MAX_BACKGROUND_BYTES:
        return "That background is larger than 100 MB."

    try:
        info = probe_video_file(path)
    except InvalidVideoError as e:
        return str(e)

    if info["duration"] > MAX_BACKGROUND_SECONDS:
        return "That background is longer than 60 seconds."
    return None


def safe_background_name(filename: str) -> str:
    """A tidy file name for backgrounds/, so uploads can never escape the folder."""
    original = Path(filename or "background")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", original.stem).strip("_") or "background"
    suffix = original.suffix.lower()
    if suffix not in BACKGROUND_EXTENSIONS:
        suffix = ".mp4"
    return f"{stem}{suffix}"


def store_background(filename: str, temporary_path: Path) -> Path:
    """Moves a checked upload into backgrounds/ without overwriting anything."""
    folder = ensure_backgrounds_dir()
    name = safe_background_name(filename)
    target = folder / name
    counter = 2
    while target.exists():
        target = folder / f"{Path(name).stem}_{counter}{Path(name).suffix}"
        counter += 1
    shutil.move(str(temporary_path), str(target))
    logger.info(f"Background clip saved as {target.name}")
    return target
