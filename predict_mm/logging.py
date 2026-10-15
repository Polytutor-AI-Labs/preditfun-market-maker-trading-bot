from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(level: str) -> logging.Logger:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/predict-mm.log", encoding="utf-8"),
        ],
    )
    return logging.getLogger("predict-mm")
