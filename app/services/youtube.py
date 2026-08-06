import os
import subprocess
import logging
import tempfile
import re
import uuid
import yt_dlp
import time

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────

# bgutil PO Token provider (runs as a sidecar container)
BGUTIL_PROVIDER_URL = os.environ.get(
    "BGUTIL_PROVIDER_URL", "http://bgutil-provider:4416"
)

# Residential rotating proxy for YouTube downloads. Read from env directly so
# it works in both the API container and the host-network worker without
# depending on the pydantic Settings object.
DOWNLOAD_PROXY_URL = os.environ.get("DOWNLOAD_PROXY_URL", "").strip()

# Sticky-session token in the proxy URL. When present, it's replaced with a
# fresh random id per download so one download holds a single IP while
# different downloads rotate.
_PROXY_SESSION_TOKEN = "{session}"


def _mask_proxy(proxy_url: str) -> str:
    """Redact credentials from a proxy URL for safe logging."""
    return re.sub(r"//[^@]+@", "//***:***@", proxy_url)


def _resolve_proxy_url() -> str | None:
    """Return the proxy URL to use for this download, or None if disabled.

    If the configured URL contains the ``{session}`` token, substitute a
    fresh random id so each download gets a sticky-but-unique upstream IP.
    """
    if not DOWNLOAD_PROXY_URL:
        return None

    proxy = DOWNLOAD_PROXY_URL
    if _PROXY_SESSION_TOKEN in proxy:
        session_id = uuid.uuid4().hex[:12]
        proxy = proxy.replace(_PROXY_SESSION_TOKEN, session_id)
    return proxy


# ── Public helpers (signatures unchanged) ──────────────────────────────────

def get_youtube_video_id(url: str) -> str:
    """
    Extract the 11-character YouTube Video ID from any YouTube URL format.

    Supports:
    - Standard: https://www.youtube.com/watch?v=VIDEO_ID
    - Shorts: https://www.youtube.com/shorts/VIDEO_ID
    - Mobile/Short: https://youtu.be/VIDEO_ID
    - Embed: https://www.youtube.com/embed/VIDEO_ID
    - With query params: ?v=VIDEO_ID&t=123&feature=share

    Args:
        url: YouTube URL in any format

    Returns:
        The 11-character video ID

    Raises:
        ValueError: If the video ID cannot be extracted
    """
    # Comprehensive regex patterns for all YouTube URL formats
    patterns = [
        r'(?:v=|/)([0-9A-Za-z_-]{11}).*',           # Standard watch, youtu.be
        r'(?:embed/)([0-9A-Za-z_-]{11})',           # Embed URLs
        r'(?:shorts/)([0-9A-Za-z_-]{11})',          # Shorts URLs
        r'^([0-9A-Za-z_-]{11})$',                   # Just the ID itself
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            # Validate it's exactly 11 characters (YouTube standard)
            if len(video_id) == 11:
                logger.info(f"Extracted YouTube ID: {video_id} from URL: {url}")
                return video_id

    raise ValueError(f"Could not extract valid YouTube video ID from URL: {url}")


# ── Private helpers ────────────────────────────────────────────────────────

def _find_cookies_file() -> str | None:
    """Find cookies.txt in known locations."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "cookies.txt"),
        "/app/cookies.txt",
        os.path.expanduser("~/cookies.txt"),
    ]
    for path in candidates:
        resolved = os.path.realpath(path)
        if os.path.isfile(resolved):
            logger.info(f"[yt-dlp] Found cookies file: {resolved}")
            return resolved
    return None


def _build_ydl_opts(
    output_template: str,
    format_spec: str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    extra_opts: dict | None = None,
) -> dict:
    """
    Build a yt-dlp options dict with all layers of YouTube bypass:
      1. Residential rotating proxy  (DOWNLOAD_PROXY_URL, with {session} sticky support)
      2. PO Token via bgutil-provider sidecar
      3. Cookies from cookies.txt

    All three layers are optional and independent: any that are unconfigured
    are simply skipped, so local/dev runs work with a plain direct connection.

    Args:
        output_template: yt-dlp output path template
        format_spec: yt-dlp format selector string
        extra_opts: additional ydl_opts to merge in
    """
    # Define a progress hook to log periodically (e.g. roughly every 10%)
    last_printed_percent = [-10]  # Use list to allow modification in nested function
    
    def progress_hook(d):
        if d['status'] == 'downloading':
            try:
                percent_str = d.get('_percent_str', '').strip()
                if not percent_str:
                    return
                    
                # Clean ANSI escape codes from percent_str if any
                import re
                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                clean_percent = ansi_escape.sub('', percent_str).replace('%', '')
                
                percent_val = float(clean_percent)
                
                # Log every 10% progress
                if percent_val - last_printed_percent[0] >= 10:
                    speed = d.get('_speed_str', 'N/A').strip()
                    eta = d.get('_eta_str', 'N/A').strip()
                    logger.info(f"[yt-dlp] Downloading... {percent_val:.1f}% at {speed} (ETA: {eta})")
                    last_printed_percent[0] = percent_val
            except Exception:
                pass
        elif d['status'] == 'finished':
            logger.info("[yt-dlp] Download finished, now finalizing...")

    opts = {
        "format": format_spec,
        "outtmpl": output_template,
        "quiet": False,
        "no_warnings": False,
        "verbose": True,
        "noprogress": True,
        "merge_output_format": "mp4",
        "postprocessor_args": ["-movflags", "+faststart"],
        # Let yt-dlp handle retries
        "retries": 3,
        "fragment_retries": 3,
        # Allow yt-dlp to download remote JS solvers for YouTube
        "remote_components": ["ejs:github"],
        # Force IPv6 ONLY when not using a proxy. Residential proxies are
        # almost always IPv4, and forcing IPv6 would break the proxy tunnel.
        "force_ipv6": not DOWNLOAD_PROXY_URL,
        # Extractor args: use web client + bgutil PO token provider
        "extractor_args": {
            "youtube": {
                "player-client": ["web", "default"],
                # Point the bgutil HTTP provider to our sidecar container
                "youtubepot-bgutilhttp:base_url": [BGUTIL_PROVIDER_URL],
            },
        },
        "progress_hooks": [progress_hook],
    }

    # Layer 1: Residential rotating proxy
    proxy_url = _resolve_proxy_url()
    if proxy_url:
        opts["proxy"] = proxy_url
        logger.info(f"[yt-dlp] Using download proxy: {_mask_proxy(proxy_url)}")

    # Layer 3: Cookies
    cookies_path = _find_cookies_file()
    if cookies_path:
        opts["cookiefile"] = cookies_path
        logger.info(f"[yt-dlp] Using cookies: {cookies_path}")

    # Merge any extra options
    if extra_opts:
        opts.update(extra_opts)

    return opts


def _seconds_to_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ── Public API (signatures preserved exactly) ─────────────────────────────

def check_video_duration(youtube_url: str) -> int:
    """Extract video duration in seconds WITHOUT downloading."""
    metadata = extract_metadata(youtube_url)
    duration = metadata.get("duration", 0)
    if not duration or duration <= 0:
        raise ValueError("Could not determine video duration")
    return int(duration)


def extract_metadata(youtube_url: str) -> dict:
    """Extract video metadata without downloading."""
    youtube_id = get_youtube_video_id(youtube_url)

    opts = _build_ydl_opts(
        output_template="/dev/null",
        extra_opts={
            "skip_download": True,
        },
    )

    logger.info(f"[yt-dlp] Extracting metadata for {youtube_id}")

    max_retries = 3
    info = None
    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
            break
        except Exception as e:
            if attempt == max_retries - 1:
                raise ValueError(f"Failed to extract metadata after {max_retries} attempts: {e}")
            logger.warning(f"Metadata extraction attempt {attempt + 1} failed: {e}. Retrying in 3s...")
            time.sleep(3)

    if not info:
        raise ValueError(f"No metadata returned for {youtube_url}")

    # Parse duration
    duration = int(info.get("duration", 0) or 0)

    # Find the highest available video height
    max_height: int | None = None
    formats = info.get("formats", [])
    for fmt in formats:
        h = fmt.get("height")
        if h and isinstance(h, (int, float)) and int(h) > (max_height or 0):
            max_height = int(h)

    # Pick best thumbnail
    thumbnails = info.get("thumbnails", [])
    thumb_url = ""
    if isinstance(thumbnails, list) and thumbnails:
        best = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))
        thumb_url = best.get("url", "")
    elif info.get("thumbnail"):
        thumb_url = info["thumbnail"]

    return {
        "title": info.get("title", ""),
        "duration": duration,
        "thumbnail": thumb_url,
        "uploader": info.get("uploader", info.get("channel", "")),
        "max_source_height": max_height,
    }


def download_audio(youtube_url: str, youtube_id: str) -> str:
    """Download audio only from a YouTube video. Returns the file path."""
    output_dir = tempfile.mkdtemp(prefix="clipper_")
    output_template = os.path.join(output_dir, f"{youtube_id}.%(ext)s")

    opts = _build_ydl_opts(
        output_template=output_template,
        format_spec="bestaudio[ext=m4a]/bestaudio/best",
        extra_opts={
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "128",
            }],
        },
    )

    logger.info(f"[yt-dlp] Downloading audio for {youtube_id}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([youtube_url])
            break
        except Exception as e:
            if attempt == max_retries - 1:
                raise ValueError(f"Failed to download audio after {max_retries} attempts: {e}")
            logger.warning(f"Download attempt {attempt + 1} failed: {e}. Retrying in 5s...")
            time.sleep(5)

    # Find the downloaded audio file
    for f in os.listdir(output_dir):
        if f.endswith((".m4a", ".mp3", ".opus", ".webm", ".ogg", ".wav")):
            audio_path = os.path.join(output_dir, f)
            logger.info(f"[Audio] Downloaded: {audio_path}")

            # If it's already m4a, return directly
            if f.endswith(".m4a"):
                return audio_path

            # Convert to m4a/AAC for Whisper compatibility
            m4a_path = os.path.join(output_dir, f"{youtube_id}_final.m4a")
            cmd = [
                "ffmpeg", "-y", "-i", audio_path,
                "-vn", "-acodec", "aac", "-b:a", "128k",
                m4a_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.warning(
                    f"[Audio] FFmpeg conversion failed, using raw: "
                    f"{result.stderr[-200:]}"
                )
                return audio_path

            os.remove(audio_path)
            logger.info(f"[Audio] Converted to AAC: {m4a_path}")
            return m4a_path

    raise FileNotFoundError(f"Audio file not found after download in {output_dir}")


def download_video_segment(
    youtube_url: str,
    start_time: float,
    end_time: float,
    quality: str = "1080p",
) -> str:
    """Download only a specific segment of the video.

    For qualities 1440p and 2160p, we prefer the exact resolution or best available
    (no upper limit) to ensure we get the highest quality source for vertical crops.
    For standard qualities (480p, 720p, 1080p), we cap at the requested height.
    """
    youtube_id = get_youtube_video_id(youtube_url)
    format_spec = _quality_to_format(quality)

    output_dir = tempfile.mkdtemp(prefix="clipper_segment_")
    full_path = os.path.join(output_dir, "full.mp4")
    output_path = os.path.join(output_dir, "segment.mp4")

    # Download the full video first
    opts = _build_ydl_opts(
        output_template=full_path,
        format_spec=format_spec,
    )

    logger.info(
        f"[yt-dlp] Downloading video {youtube_id} at {quality} for segment "
        f"{start_time:.1f}-{end_time:.1f}s"
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([youtube_url])
            break
        except Exception as e:
            if attempt == max_retries - 1:
                raise ValueError(f"Failed to download video segment after {max_retries} attempts: {e}")
            logger.warning(f"Segment download attempt {attempt + 1} failed: {e}. Retrying in 5s...")
            time.sleep(5)

    # yt-dlp may append an extension — find the actual file
    actual_full = _find_downloaded_file(output_dir, "full")
    if not actual_full:
        raise FileNotFoundError("Downloaded video not found after yt-dlp")

    # Cut segment with FFmpeg
    logger.info(
        f"[Segment] Cutting {start_time:.1f}-{end_time:.1f}s from {quality}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time),
        "-to", str(end_time),
        "-i", actual_full,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg segment cut failed: {result.stderr[-300:]}")

    os.remove(actual_full)

    if os.path.exists(output_path):
        return output_path

    # Fallback: find any mp4
    for f in os.listdir(output_dir):
        if f.endswith(".mp4"):
            return os.path.join(output_dir, f)

    raise FileNotFoundError("Segment file not found after download")


def download_full_video(
    youtube_url: str,
    youtube_id: str,
    quality: str = "best",
) -> str:
    """
    Download the full video (not a segment) for caching purposes.

    This is used to cache the source video in S3 for future clip rendering.
    Downloads the highest quality available up to the requested quality.

    Args:
        youtube_url: The YouTube URL
        youtube_id: The YouTube video ID (used for filename)
        quality: Target quality (best, 2160p, 1440p, 1080p, 720p, 480p)

    Returns:
        Path to the downloaded video file
    """
    format_spec = _quality_to_format(quality)

    output_dir = tempfile.mkdtemp(prefix="clipper_full_")
    output_path = os.path.join(output_dir, f"{youtube_id}.mp4")

    opts = _build_ydl_opts(
        output_template=output_path,
        format_spec=format_spec,
    )

    logger.info(f"[yt-dlp] Downloading full video: {youtube_id} at quality={quality}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([youtube_url])
            break
        except Exception as e:
            if attempt == max_retries - 1:
                raise ValueError(f"Failed to download full video after {max_retries} attempts: {e}")
            logger.warning(f"Full download attempt {attempt + 1} failed: {e}. Retrying in 5s...")
            time.sleep(5)

    # yt-dlp may change the extension — find the actual file
    if os.path.exists(output_path):
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(
            f"[Cache] Downloaded full video: {output_path} ({file_size_mb:.1f} MB)"
        )
        return output_path

    # Fallback: find any video file in the directory
    for f in os.listdir(output_dir):
        if f.endswith((".mp4", ".mkv", ".webm")):
            full_path = os.path.join(output_dir, f)
            file_size_mb = os.path.getsize(full_path) / (1024 * 1024)
            logger.info(f"[Cache] Found video file: {full_path} ({file_size_mb:.1f} MB)")
            return full_path

    raise FileNotFoundError(f"Video file not found after download in {output_dir}")


# ── Internal utilities ─────────────────────────────────────────────────────

def _quality_to_format(quality: str) -> str:
    """Convert a quality string to a yt-dlp format selector."""
    QUALITY_MAP = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "2160p": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best",
        "1440p": "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
        "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
        "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
    }
    return QUALITY_MAP.get(quality, QUALITY_MAP["1080p"])


def _find_downloaded_file(directory: str, name_prefix: str) -> str | None:
    """Find a downloaded file by prefix (yt-dlp may change extensions)."""
    for f in os.listdir(directory):
        if f.startswith(name_prefix) and f.endswith((".mp4", ".mkv", ".webm")):
            return os.path.join(directory, f)
    # Broader fallback
    for f in os.listdir(directory):
        if f.endswith((".mp4", ".mkv", ".webm")):
            return os.path.join(directory, f)
    return None
