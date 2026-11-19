"""
Whisper model helpers shared by the CLI and the transcriber.

faster-whisper downloads models from Hugging Face on first use. In an installed
build we keep them in %LOCALAPPDATA%\\Trimly\\models; in a source checkout the
default Hugging Face cache is used, as before.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.paths import IS_INSTALLED, MODELS_DIR

# Approximate download sizes, used in the first-run message and doctor output.
MODEL_SIZES_MB = {
    "tiny": 75,
    "base": 145,
    "small": 484,
    "medium": 1530,
    "large": 3090,
    "large-v3": 3090,
}
MODEL_REPOS = "Systran/faster-whisper-{model}"
DEFAULT_HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"


def model_size_mb(model: str) -> int:
    return MODEL_SIZES_MB.get(model.strip().lower(), 145)


def whisper_cache_root() -> Path | None:
    """Folder passed to faster-whisper as download_root (None = HF default)."""
    override = os.environ.get("WHISPER_DOWNLOAD_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return MODELS_DIR if IS_INSTALLED else None


def cache_folder(model: str) -> Path:
    root = whisper_cache_root() or DEFAULT_HF_CACHE
    return root / f"models--Systran--faster-whisper-{model.strip().lower()}"


def is_model_downloaded(model: str) -> bool:
    """True when a model.bin snapshot already exists on this computer."""
    folder = cache_folder(model)
    if not folder.is_dir():
        return False
    return any(folder.glob("snapshots/*/model.bin"))


def download_model(model: str) -> Path | None:
    """
    Downloads a Whisper model once. The Hugging Face client prints a progress
    bar (size and speed), which is exactly what a first run should show.
    """
    model = model.strip().lower()
    if is_model_downloaded(model):
        return cache_folder(model)

    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise RuntimeError(
            "huggingface_hub is missing. Reinstall with: uv tool install --force "
            "git+https://github.com/USERNAME/trimly.git"
        ) from e

    root = whisper_cache_root()
    snapshot = snapshot_download(
        repo_id=MODEL_REPOS.format(model=model),
        cache_dir=str(root) if root else None,
    )
    return Path(snapshot)
