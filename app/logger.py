"""
Central logging configuration for Cliptor.
Logs to both stdout (terminal) and to a persistent file (cliptor.log).
"""
import logging
import sys
from pathlib import Path
from app.config import BASE_DIR

LOG_FILE = BASE_DIR / "cliptor.log"

def setup_logger() -> logging.Logger:
    """Configures the root/app logger with console and file handlers."""
    logger = logging.getLogger("cliptor")
    logger.setLevel(logging.INFO)

    # Avoid adding duplicate handlers if already configured
    if logger.handlers:
        return logger

    # Format for log records: timestamp, level, module, message
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. Console handler (writes to terminal)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 2. File handler (writes to cliptor.log)
    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

# Initialize logger on import
logger = setup_logger()
