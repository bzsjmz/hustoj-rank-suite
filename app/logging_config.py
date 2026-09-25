from __future__ import annotations

import logging
import copy
import traceback
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


from .session import sanitize_log_text


class SafeFormatter(logging.Formatter):
    def formatException(self, exc_info) -> str:
        # Exception messages and chained exceptions can contain request headers,
        # cookies or login URLs. Preserve locations and type, never their payload.
        frames = traceback.extract_tb(exc_info[2])
        locations = "\n".join(f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames)
        return locations + "\n" + exc_info[0].__name__

    def format(self, record) -> str:
        record = copy.copy(record)
        record.msg = sanitize_log_text(record.getMessage())
        record.args = ()
        record.exc_text = None  # Do not reuse another handler's unsafe traceback.
        return super().format(record)


def configure_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("oj_rank")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = SafeFormatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)

    logfile = RotatingFileHandler(
        log_dir / "crawler.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    logfile.setFormatter(formatter)
    logger.addHandler(stdout)
    logger.addHandler(logfile)
    return logger
