"""
Where Trimly keeps its files.

Two modes, detected automatically:

* **source mode** - you are running from the git checkout. Data stays next to
  the code (jobs_data/, logs/, backgrounds/) and settings are read from .env.
* **installed mode** - you installed the ``trimly`` command with uv tool, pipx
  or pip. The web assets (templates, static, fonts) are inside the package, and
  all writable data goes to the user profile instead:
      %LOCALAPPDATA%\\Trimly   jobs, logs, Whisper models, backgrounds
      %APPDATA%\\Trimly\\config.env   settings

Environment variables always win over the config file. Two extra variables are
handy for tests or custom setups: TRIMLY_DATA_DIR and TRIMLY_CONFIG_FILE.
"""
from __future__ import annotations

import os
from pathlib import Path

# app/paths.py -> app/  and  the repo root (or site-packages) above it.
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent

# A wheel always contains all three asset folders inside the package. That is
# how we tell an installed build apart from the source checkout.
IS_INSTALLED = (
    (PACKAGE_DIR / "templates").is_dir()
    and (PACKAGE_DIR / "static").is_dir()
    and (PACKAGE_DIR / "fonts").is_dir()
)

# Never use the current working directory: the app may be started anywhere.
ASSETS_DIR = PACKAGE_DIR if IS_INSTALLED else SOURCE_ROOT
TEMPLATES_DIR = ASSETS_DIR / "templates"
STATIC_DIR = ASSETS_DIR / "static"
FONTS_DIR = ASSETS_DIR / "fonts"


def _env_path(name: str) -> Path | None:
    """Returns an environment variable as a path, or None when it is empty."""
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def _data_dir() -> Path:
    override = _env_path("TRIMLY_DATA_DIR")
    if override:
        return override
    if IS_INSTALLED:
        # LOCALAPPDATA on Windows; a sensible fallback on other systems.
        local = _env_path("LOCALAPPDATA") or (Path.home() / ".local" / "share")
        return local / "Trimly"
    return SOURCE_ROOT


def _config_file() -> Path:
    override = _env_path("TRIMLY_CONFIG_FILE")
    if override:
        return override
    if IS_INSTALLED:
        roaming = _env_path("APPDATA") or (Path.home() / ".config")
        return roaming / "Trimly" / "config.env"
    return SOURCE_ROOT / ".env"


# All writable paths used by the app.
DATA_DIR = _data_dir()
JOBS_DIR = DATA_DIR / "jobs_data"
LOGS_DIR = DATA_DIR / "logs"
MODELS_DIR = DATA_DIR / "models"
BACKGROUNDS_DIR = DATA_DIR / "backgrounds"
LOG_FILE = DATA_DIR / "trimly.log"
CONFIG_FILE = _config_file()


def ensure_user_dirs() -> None:
    """Creates the writable folders once, at startup. Never inside the package."""
    folders = [DATA_DIR, JOBS_DIR, LOGS_DIR]
    if IS_INSTALLED:
        # Model cache and background clips only live in the user profile.
        folders += [MODELS_DIR, BACKGROUNDS_DIR]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
