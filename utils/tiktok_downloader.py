import os
from typing import Tuple


async def download_tiktok_with_fallbacks(url: str, output_dir: str) -> Tuple[bool, str, str]:
    """Placeholder TikTok downloader.

    Returns (success, method_used, result_path_or_message).
    Always fails to force fallback to yt-dlp in utils/downloader.py.
    """
    return False, "placeholder", "Not implemented"


def tiktok_downloader(url: str, output_dir: str) -> str:
    """Synchronous placeholder for compatibility if called elsewhere."""
    raise NotImplementedError("Custom TikTok downloader is not implemented on Railway build.")


