"""
Central logging configuration for Trimly.
Logs to both stdout (terminal) and to a persistent file (trimly.log).
"""
import logging
import sys
from app.paths import LOG_FILE, ensure_user_dirs

def setup_logger() -> logging.Logger:
    """Configures the root/app logger with console and file handlers."""
    ensure_user_dirs()  # in case the logger is imported before config
    logger = logging.getLogger("trimly")
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

    # 2. File handler (writes to trimly.log)
    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

# Initialize logger on import
logger = setup_logger()
