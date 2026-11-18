"""
Caption font discovery and looks for Cliptor Clip Studio.

The fonts/ folder is scanned once when this module is imported (so at server
startup).  Each font's real family name and weight are read with fontTools
instead of being guessed from the file name, because libass silently falls
back to a default font when the family name does not match.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from fontTools.ttLib import TTFont
except Exception:  # pragma: no cover - fontTools is in requirements.txt
    TTFont = None

# app/fonts.py -> project root
BASE_DIR = Path(__file__).resolve().parent.parent
FONTS_DIR = BASE_DIR / "fonts"

# The current caption look, kept as "Plain".
PLAIN_STYLE: Dict[str, Any] = {
    "key": "plain",
    "display_name": "Plain",
    "label": "Plain (current look)",
    "family": "Arial",
    "filename": None,
    "weight": 400,
    "uppercase": False,
    "size_multiplier": 1.0,
    "outline": 5.0,
    "shadow": 0.0,
    "highlight": False,
    "margin_v": 260,
    "bold": False,
    "css_family": "Arial",
}

# Hand-tuned looks, keyed by font file name. Anything not listed gets the
# neutral defaults below, so adding another .ttf still works.
TUNING: Dict[str, Dict[str, Any]] = {
    "Montserrat-ExtraBold.ttf": {
        "uppercase": False,      # keep normal casing, just very heavy
        "size_multiplier": 1.0,
        "outline": 5.0,
        "shadow": 2.0,
        "highlight": True,        # current word is yellow
        "margin_v": 500,          # lower third, about 70-75% down the frame
        "bold": True,
        "label": "Montserrat ExtraBold (Bold)",
    },
    "BebasNeue-Regular.ttf": {
        "uppercase": True,
        "size_multiplier": 1.18,
        "outline": 4.0,
        "shadow": 1.0,
    },
    "Anton-Regular.ttf": {
        "uppercase": True,
        "size_multiplier": 1.12,
        "outline": 5.0,
        "shadow": 1.0,
    },
    "Oswald-Bold.ttf": {
        "uppercase": True,
        "size_multiplier": 1.06,
        "outline": 5.0,
        "shadow": 1.0,
    },
    "Bangers-Regular.ttf": {
        "size_multiplier": 1.08,
        "outline": 5.0,
        "shadow": 2.0,
    },
    "LuckiestGuy-Regular.ttf": {
        "size_multiplier": 1.0,
        "outline": 5.0,
        "shadow": 2.0,
    },
    "PermanentMarker-Regular.ttf": {
        "size_multiplier": 1.0,
        "outline": 3.0,          # thinner marker outline
        "shadow": 1.0,
    },
    "Rubik-Black.ttf": {
        "size_multiplier": 0.98,
        "outline": 5.0,
        "shadow": 1.0,
    },
    "PressStart2P-Regular.ttf": {
        "uppercase": True,
        "size_multiplier": 0.60,  # pixel font needs much less height
        "outline": 4.0,
        "shadow": 1.0,
    },
}

DEFAULTS: Dict[str, Any] = {
    "uppercase": False,
    "size_multiplier": 1.0,
    "outline": 5.0,
    "shadow": 0.0,
    "highlight": False,
    "margin_v": 260,
    "bold": False,
}

# Order shown in the UI. The default "Bold" style comes first.
PREFERRED_ORDER = [
    "Montserrat-ExtraBold.ttf",
    "BebasNeue-Regular.ttf",
    "Anton-Regular.ttf",
    "Oswald-Bold.ttf",
    "Bangers-Regular.ttf",
    "LuckiestGuy-Regular.ttf",
    "PermanentMarker-Regular.ttf",
    "Rubik-Black.ttf",
    "PressStart2P-Regular.ttf",
]

DEFAULT_FONT_STYLE = "montserrat_extrabold"


@dataclass
class FontInfo:
    filename: str
    family: str
    subfamily: str
    weight: int


def _slug(text: str) -> str:
    """'Montserrat ExtraBold' -> 'montserrat_extrabold' (used as the style key)."""
    cleaned = [ch.lower() if ch.isalnum() else "_" for ch in text]
    return "".join(cleaned).strip("_").replace("__", "_")


def _read_font(path: Path) -> Optional[FontInfo]:
    """Reads the real family/subfamily/weight from the font file itself."""
    if TTFont is None:
        return None
    try:
        font = TTFont(str(path), lazy=True)
    except Exception:
        return None

    try:
        name = font["name"]
        family = (name.getDebugName(1) or "").strip()
        subfamily = (name.getDebugName(2) or "Regular").strip()
        weight = 400
        try:
            weight = int(font["OS/2"].usWeightClass)
        except Exception:
            pass
        if not family:
            return None
        return FontInfo(
            filename=path.name,
            family=family,
            subfamily=subfamily,
            weight=weight,
        )
    finally:
        try:
            font.close()
        except Exception:
            pass


def _display_name(info: FontInfo) -> str:
    """Adds the weight only when the family name does not already contain it."""
    if info.subfamily.lower() in {"regular", "normal", ""}:
        return info.family
    if info.subfamily.lower() in info.family.lower():
        return info.family
    return f"{info.family} {info.subfamily}"


def scan_fonts() -> List[FontInfo]:
    """Every .ttf in fonts/, in a stable order. Read once at import time."""
    if not FONTS_DIR.exists():
        return []

    found: List[FontInfo] = []
    for path in sorted(FONTS_DIR.glob("*.ttf")):
        info = _read_font(path)
        if info is not None:
            found.append(info)

    def sort_key(info: FontInfo):
        try:
            return (PREFERRED_ORDER.index(info.filename), info.filename)
        except ValueError:
            return (len(PREFERRED_ORDER), info.filename)

    found.sort(key=sort_key)
    return found


def _build_styles() -> Dict[str, Dict[str, Any]]:
    styles: Dict[str, Dict[str, Any]] = {"plain": dict(PLAIN_STYLE)}
    for info in scan_fonts():
        tuning = dict(DEFAULTS)
        tuning.update(TUNING.get(info.filename, {}))
        display = _display_name(info)
        key = _slug(display)
        styles[key] = {
            "key": key,
            "display_name": display,
            "label": tuning.pop("label", display),
            "family": info.family,
            "filename": info.filename,
            "weight": info.weight,
            "subfamily": info.subfamily,
            "css_family": f"cliptor-{key}",
            **tuning,
        }
    return styles


# Scanned once, at startup.
FONT_STYLES: Dict[str, Dict[str, Any]] = _build_styles()

# If the exact key is missing (for example a font was deleted), fall back to
# Plain instead of crashing a render.
if DEFAULT_FONT_STYLE not in FONT_STYLES and "plain" in FONT_STYLES:
    DEFAULT_FONT_STYLE = "plain"


def get_font_style(key: Optional[str]) -> Dict[str, Any]:
    """Returns a style dict by key, or the default when the key is unknown."""
    if key and key in FONT_STYLES:
        return dict(FONT_STYLES[key])
    return dict(FONT_STYLES.get(DEFAULT_FONT_STYLE, PLAIN_STYLE))


def list_font_styles() -> List[Dict[str, Any]]:
    """Plain first, then every font in display order (for the template)."""
    return [dict(style) for style in FONT_STYLES.values()]


def copy_font_to_job(font_filename: Optional[str], job_dir: Path) -> Optional[Path]:
    """
    Copies only the font in use into <job>/fonts/ so ffmpeg can load it with
    fontsdir=fonts (a relative path, no Windows escaping needed).
    """
    if not font_filename:
        return None

    source = FONTS_DIR / font_filename
    if not source.exists():
        return None

    destination_dir = Path(job_dir) / "fonts"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / font_filename
    if not destination.exists() or destination.stat().st_size != source.stat().st_size:
        shutil.copyfile(source, destination)
    return destination
