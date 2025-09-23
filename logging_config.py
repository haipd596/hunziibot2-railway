import logging
import os


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


    # Silence chatty HTTP client logs unless explicitly enabled
    httpx_level_name = os.getenv("LOG_HTTPX_LEVEL", "WARNING").upper()
    httpx_level = getattr(logging, httpx_level_name, logging.WARNING)
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpx_level)
