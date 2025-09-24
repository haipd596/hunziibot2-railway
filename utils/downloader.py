import os
import re
import asyncio
import hashlib
import time
from urllib.parse import urlparse
import yt_dlp
import requests
import subprocess
import shlex
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging

logger = logging.getLogger(__name__)

# Temporary storage for URLs that are too long for callback data
_url_cache = {}
_cache_cleanup_time = 3600  # 1 hour

# Import the new TikTok downloader
try:
    from .tiktok_downloader import download_tiktok_with_fallbacks, tiktok_downloader
    TIKTOK_DOWNLOADER_AVAILABLE = True
except ImportError:
    TIKTOK_DOWNLOADER_AVAILABLE = False
    logger.warning("TikTok downloader not available, falling back to yt-dlp")

# Enhanced TikTok downloader disabled due to httpx version conflict
ENHANCED_TIKTOK_AVAILABLE = False
logger.warning("Enhanced TikTok downloader disabled due to httpx version conflict")

# URL patterns for different platforms
PLATFORM_PATTERNS = {
    'facebook': r'(?:https?://)?(?:www\.)?(?:facebook\.com|fb\.com|m\.facebook\.com)',
    'instagram': r'(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)',
    'tiktok': r'(?:https?://)?(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com)',
    'douyin': r'(?:https?://)?(?:www\.)?(?:douyin\.com|iesdouyin\.com)',
    'youtube': r'(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be|m\.youtube\.com)',
    'twitter': r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com|t\.co)',
    'reddit': r'(?:https?://)?(?:www\.)?(?:reddit\.com|redd\.it)',
    'pinterest': r'(?:https?://)?(?:www\.)?(?:pinterest\.com|pin\.it)',
    'qqmusic': r'(?:https?://)?(?:www\.)?(?:y\.qq\.com|i\.y\.qq\.com)',
}

# Prefer using Piped API for YouTube if available to avoid cookie challenges
YOUTUBE_PIPED_ENABLED = os.getenv('YOUTUBE_PIPED_ENABLED', 'true').lower() in ('1', 'true', 'yes')
PIPED_INSTANCES = [
    'https://piped.video',
    'https://piped.mha.fi',
    'https://piped.projectsegfau.lt',
]

def detect_platform(url: str) -> str:
    """Detect platform from URL"""
    for platform, pattern in PLATFORM_PATTERNS.items():
        if re.search(pattern, url):
            return platform
    return 'unknown'

def get_download_path(platform: str, filename: str) -> str:
    """Get download path based on platform"""
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    media_dir = os.path.join(base_dir, "data", "media", platform)
    os.makedirs(media_dir, exist_ok=True)
    return os.path.join(media_dir, filename)

def build_ydl_opts(platform: str, outtmpl: str, audio_only: bool = False) -> dict:
    """Build yt-dlp options per platform, with better reliability for TikTok.
    If data/cookies.txt exists, it will be used automatically.
    """
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    opts = {
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
        'concurrent_fragment_downloads': 1,
        'socket_timeout': 24,  # Increased by 20% from 20
        'retries': 3,
    }
    if audio_only:
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }
        ]
    else:
        # Prefer mp4 container for better Telegram compatibility; fall back to best
        opts['format'] = 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b'
        opts['merge_output_format'] = 'mp4'

    # Optional cookie support
    cookie_file = os.path.join(base_dir, 'data', 'cookies.txt')
    if os.path.exists(cookie_file):
        opts['cookiefile'] = cookie_file

    # Platform-specific tweaks (excluding TikTok - now handled separately)
    if platform in ['facebook', 'instagram', 'youtube']:
        opts['geo_bypass'] = True

    # YouTube-specific robustness: use Android client to bypass some age/consent walls
    if platform == 'youtube':
        opts.setdefault('extractor_args', {})
        opts['extractor_args'].setdefault('youtube', {})
        opts['extractor_args']['youtube']['player_client'] = ['android']

    return opts

def _extract_youtube_id(url: str) -> str | None:
    try:
        # Handle various YouTube URL formats
        m = re.search(r"(?:v=|/shorts/|/live/|youtu\.be/)([A-Za-z0-9_-]{6,})", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

def _select_best_piped_stream(streams: list[dict]) -> dict | None:
    # Prefer MP4 video with audio if available; fall back to highest quality MP4
    mp4_streams = [s for s in streams if (s.get('container') == 'mp4' or (s.get('mimeType') or '').startswith('video/mp4'))]
    if not mp4_streams:
        return None
    def parse_quality(s: dict) -> int:
        q = s.get('qualityLabel') or s.get('quality') or ''
        m = re.search(r"(\d+)", q)
        return int(m.group(1)) if m else 0
    mp4_streams.sort(key=parse_quality, reverse=True)
    return mp4_streams[0]

def _download_file(url: str, dest_path: str, headers: dict | None = None, timeout: int = 30) -> bool:
    try:
        with requests.get(url, stream=True, headers=headers or {'User-Agent': 'Mozilla/5.0'}, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Piped download failed: {e}")
        return False

def _download_youtube_via_piped(url: str, platform: str) -> str | None:
    video_id = _extract_youtube_id(url)
    if not video_id:
        return None
    for base in PIPED_INSTANCES:
        try:
            # Use streams endpoint which exposes separate video/audio URLs
            api_url = f"{base}/api/v1/streams/{video_id}"
            resp = requests.get(api_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            if resp.status_code != 200:
                continue
            data = resp.json()
            title = data.get('title') or video_id
            v_streams = data.get('videoStreams') or []
            a_streams = data.get('audioStreams') or []

            # Try muxed stream first (videoOnly == False)
            muxed = [s for s in v_streams if not s.get('videoOnly')]
            def quality_key(s: dict) -> int:
                q = s.get('qualityLabel') or s.get('quality') or ''
                m = re.search(r"(\d+)", q)
                return int(m.group(1)) if m else 0
            muxed.sort(key=quality_key, reverse=True)

            out_dir = get_download_path(platform, '')
            safe_title = re.sub(r"[\\/:*?\"<>|]", "_", title)
            if muxed:
                dest_path = os.path.join(out_dir, f"{safe_title}.mp4")
                if _download_file(muxed[0].get('url'), dest_path):
                    return dest_path

            # Fallback: pick best videoOnly + best audio and merge via ffmpeg
            video_only = [s for s in v_streams if s.get('videoOnly')]
            video_only.sort(key=quality_key, reverse=True)
            # Prefer m4a/mp4 audio
            def audio_rank(a: dict) -> tuple[int, int]:
                mime = (a.get('mimeType') or '').lower()
                # prefer m4a/aac, then anything else
                score = 2 if ('mp4' in mime or 'm4a' in mime or 'aac' in mime) else 1
                abr = a.get('bitrate') or 0
                return (score, int(abr))
            a_streams.sort(key=audio_rank, reverse=True)

            if video_only and a_streams:
                v = video_only[0]
                a = a_streams[0]
                v_path = os.path.join(out_dir, f"{safe_title}.video.mp4")
                a_ext = '.m4a' if ('m4a' in (a.get('mimeType') or '').lower()) else '.audio'
                a_path = os.path.join(out_dir, f"{safe_title}{a_ext}")
                final_path = os.path.join(out_dir, f"{safe_title}.mp4")
                if not _download_file(v.get('url'), v_path):
                    continue
                if not _download_file(a.get('url'), a_path):
                    try:
                        os.remove(v_path)
                    except Exception:
                        pass
                    continue
                try:
                    cmd = f"ffmpeg -y -i {shlex.quote(v_path)} -i {shlex.quote(a_path)} -c copy {shlex.quote(final_path)}"
                    proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if proc.returncode == 0 and os.path.exists(final_path):
                        try:
                            os.remove(v_path)
                            os.remove(a_path)
                        except Exception:
                            pass
                        return final_path
                except Exception as e:
                    logger.error(f"ffmpeg merge failed: {e}")
                # cleanup on failure
                try:
                    if os.path.exists(v_path):
                        os.remove(v_path)
                    if os.path.exists(a_path):
                        os.remove(a_path)
                except Exception:
                    pass
        except Exception as e:
            logger.info(f"Piped instance failed {base}: {e}")
            continue
    return None

def _cleanup_url_cache():
    """Clean up expired entries from URL cache"""
    current_time = time.time()
    expired_keys = [key for key, (_, _, timestamp) in _url_cache.items() \
                   if current_time - timestamp > _cache_cleanup_time]
    for key in expired_keys:
        del _url_cache[key]

def _store_url_in_cache(url: str, platform: str) -> str:
    """Store URL in cache and return a short hash key"""
    _cleanup_url_cache()
    url_hash = hashlib.md5(f"{platform}|{url}".encode()).hexdigest()[:8]
    _url_cache[url_hash] = (url, platform, time.time())
    return url_hash

def _get_url_from_cache(url_hash: str) -> tuple:
    """Get URL and platform from cache by hash"""
    if url_hash in _url_cache:
        cached_data = _url_cache[url_hash]
        if len(cached_data) == 3:
            url, platform, _ = cached_data
        else:
            url, platform = cached_data
        return url, platform
    return None, None

def build_action_keyboard(original_url: str, platform: str) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="HD Download", url=original_url),
            InlineKeyboardButton(text="Origin URL", url=original_url),
        ]
    ]
    callback_data = f"convert_audio|{platform}|{original_url}"
    if len(callback_data.encode('utf-8')) <= 64:
        buttons.append([
            InlineKeyboardButton(text="Convert to Audio", callback_data=callback_data)
        ])
    else:
        url_hash = _store_url_in_cache(original_url, platform)
        buttons.append([
            InlineKeyboardButton(text="Convert to Audio", callback_data=f"convert_audio_cached|{url_hash}")
        ])
    return InlineKeyboardMarkup(buttons)

async def send_file_with_buttons(update: Update, platform: str, file_path: str, original_url: str) -> None:
    try:
        keyboard = build_action_keyboard(original_url, platform)
        filename = os.path.basename(file_path)
        with open(file_path, 'rb') as f:
            if filename.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')):
                await update.message.reply_video(f, reply_markup=keyboard)
            elif filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                await update.message.reply_photo(f, reply_markup=keyboard)
            elif filename.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.aac', '.ogg')):
                await update.message.reply_audio(f, reply_markup=keyboard)
            else:
                await update.message.reply_document(f, reply_markup=keyboard)
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass

async def send_files_with_buttons(update: Update, platform: str, file_paths: list[str], original_url: str) -> None:
    keyboard = build_action_keyboard(original_url, platform)
    try:
        for file_path in file_paths:
            try:
                filename = os.path.basename(file_path)
                with open(file_path, 'rb') as f:
                    if filename.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')):
                        await update.message.reply_video(f, reply_markup=keyboard)
                    elif filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                        await update.message.reply_photo(f, reply_markup=keyboard)
                    elif filename.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.aac', '.ogg')):
                        await update.message.reply_audio(f, reply_markup=keyboard)
                    else:
                        await update.message.reply_document(f, reply_markup=keyboard)
            finally:
                try:
                    os.remove(file_path)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error sending multiple files: {e}")

async def download_tiktok_special(url: str, platform: str, update: Update) -> bool:
    try:
        output_dir = get_download_path(platform, "")
        if TIKTOK_DOWNLOADER_AVAILABLE:
            try:
                success, method, result = await download_tiktok_with_fallbacks(url, output_dir)
                if success and os.path.exists(result):
                    all_files = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f))]
                    video_files = [f for f in all_files if f.lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm'))]
                    if video_files:
                        files_sorted = sorted(video_files, key=lambda p: os.path.getctime(p), reverse=False)
                        if len(files_sorted) == 1:
                            await send_file_with_buttons(update, platform, files_sorted[0], url)
                        else:
                            await send_files_with_buttons(update, platform, files_sorted, url)
                        return True
            except Exception as e:
                logger.error(f"Custom TikTok downloader error: {e}")
        logger.info("Using yt-dlp fallback with video-only options")
        try:
            ydl_opts = {
                'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
                'format': 'best[ext=mp4]/best[ext=webm]/best[ext=mov]/best[ext=avi]/best[ext=mkv]/best',
                'writesubtitles': False,
                'writethumbnail': False,
                'writeinfojson': False,
                'writedescription': False,
                'writeannotations': False,
                'writeautomaticsub': False,
                'ignoreerrors': False,
                'no_warnings': True,
                'quiet': True
            }
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))
            if result == 0:
                video_files = [f for f in os.listdir(output_dir) if f.lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm'))]
                if video_files:
                    latest_video = max(video_files, key=lambda x: os.path.getctime(os.path.join(output_dir, x)))
                    file_path = os.path.join(output_dir, latest_video)
                    await send_file_with_buttons(update, platform, file_path, url)
                    return True
                else:
                    return False
            else:
                return False
        except Exception as e:
            logger.error(f"yt-dlp fallback error: {e}")
            return False
    except Exception as e:
        logger.error(f"TikTok special download error: {e}")
        return False

async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args).strip()
    if not url and update.message and update.message.reply_to_message:
        url = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not url:
        return
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, url)
    if not urls:
        return
    platform = detect_platform(urls[0])
    if platform == 'unknown':
        return
    if platform in ['tiktok', 'douyin']:
        success = await download_tiktok_special(urls[0], platform, update)
    else:
        success = await download_direct(urls[0], platform, update)
    if not success:
        pass

async def download_direct(url: str, platform: str, update: Update) -> bool:
    try:
        # Prefer Piped for YouTube if enabled
        if platform == 'youtube' and YOUTUBE_PIPED_ENABLED:
            path = _download_youtube_via_piped(url, platform)
            if path and os.path.exists(path):
                await send_file_with_buttons(update, platform, path, url)
                return True

        ydl_opts = build_ydl_opts(platform, get_download_path(platform, '%(title)s.%(ext)s'))
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))
        if result == 0:
            media_dir = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "data", "media", platform)
            files = [f for f in os.listdir(media_dir) if os.path.isfile(os.path.join(media_dir, f))]
            if files:
                latest_file = max(files, key=lambda x: os.path.getctime(os.path.join(media_dir, x)))
                file_path = os.path.join(media_dir, latest_file)
                await send_file_with_buttons(update, platform, file_path, url)
                return True
            else:
                return True
        else:
            return False
    except Exception as e:
        logger.error(f"Error in direct download: {e}")
        return False

async def download_urls_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    if not text:
        return
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)
    if not urls:
        return
    supported_urls = [url for url in urls if detect_platform(url) != 'unknown']
    if supported_urls:
        url = supported_urls[0]
        platform = detect_platform(url)
        if platform in ['tiktok', 'douyin']:
            success = await download_tiktok_special(url, platform, update)
        else:
            success = await download_direct(url, platform, update)
        if not success:
            pass

async def download_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text and update.message and update.message.reply_to_message:
        text = (update.message.reply_to_message.text or update.message.reply_to_message.caption or "").strip()
    if not text:
        return
    urls = re.findall(r'https?://[^\s]+', text)
    if not urls:
        return
    for url in urls:
        platform = detect_platform(url)
        try:
            if platform in ['tiktok', 'douyin']:
                ok = await download_tiktok_special(url, platform, update)
            elif platform != 'unknown':
                ok = await download_direct(url, platform, update)
            else:
                ok = False
        except Exception:
            pass

async def handle_convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        data = query.data
        if data.startswith("convert_audio_cached|"):
            parts = data.split('|', 1)
            if len(parts) != 2:
                raise ValueError("Invalid cached callback data format")
            url_hash = parts[1]
            original_url, platform = _get_url_from_cache(url_hash)
            if not original_url:
                await query.edit_message_reply_markup(reply_markup=None)
                return
        else:
            parts = data.split('|', 2)
            if len(parts) != 3:
                raise ValueError("Invalid direct callback data format")
            _, platform, original_url = parts
    except Exception:
        await query.edit_message_reply_markup(reply_markup=None)
        return
    try:
        outtmpl = get_download_path(platform or 'youtube', '%(title)s.%(ext)s')
        ydl_opts = build_ydl_opts(platform or 'youtube', outtmpl, audio_only=True)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([original_url]))
        if result == 0:
            media_dir = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "data", "media", platform or 'youtube')
            files = [f for f in os.listdir(media_dir) if os.path.isfile(os.path.join(media_dir, f))]
            if files:
                latest_file = max(files, key=lambda x: os.path.getctime(os.path.join(media_dir, x)))
                file_path = os.path.join(media_dir, latest_file)
                try:
                    keyboard = build_action_keyboard(original_url, platform)
                    with open(file_path, 'rb') as f:
                        await query.message.reply_audio(f, reply_markup=keyboard)
                finally:
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"Convert to audio error: {e}")

async def download_urls_from_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return
    text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not text:
        return
    urls = re.findall(r'https?://[^\s]+', text)
    if not urls:
        return
    supported_urls = []
    for url in urls:
        platform = detect_platform(url)
        if platform != 'unknown':
            supported_urls.append(url)
    if supported_urls:
        url = supported_urls[0]
        platform = detect_platform(url)
        if platform in ['tiktok', 'douyin']:
            success = await download_tiktok_special(url, platform, update)
        else:
            success = await download_direct(url, platform, update)
        if not success:
            pass


