"""日志：同时输出到控制台和滚动文件。"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import app_dir

_configured = False


def setup(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    log_dir = app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    fh = logging.handlers.RotatingFileHandler(
        log_dir / "mangakit.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # httpx/httpcore 日志噪音太大
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)