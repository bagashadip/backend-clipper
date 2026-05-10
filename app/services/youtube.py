import os
import subprocess
import logging
import tempfile
import re
import requests

logger = logging.getLogger(__name__)

# ── RapidAPI YT-API configuration (metadata only) ─────────────────────────
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "yt-api.p.rapidapi.com"
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"

# ── Cobalt API configuration (actual downloads) ───────────────────────────
# Self-hosted Cobalt instance that tunnels/proxies downloads through its
# own server, eliminating googlevideo.com IP-restriction 403 errors.
COBALT_API_URL = os.environ.get("COBALT_API_URL", "http://cobalt:9000")

# Chunk size for streaming downloads (1 MB)
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


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

def _get_rapidapi_headers() -> dict:
    """Return the RapidAPI request headers for metadata calls."""
    if not RAPIDAPI_KEY:
        raise ValueError(
            "RAPIDAPI_KEY environment variable is not set. "
            "Add it to your .env / docker-compose environment."
        )
    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }


def _fetch_yt_api_metadata(youtube_id: str) -> dict:
    """
    Call YT-API for metadata only (title, duration, thumbnails, formats list).
    We use this for extract_metadata() since it's a lightweight JSON call.
    """
    url = f"{RAPIDAPI_BASE}/dl"
    params = {"id": youtube_id}
    headers = _get_rapidapi_headers()

    logger.info(f"[YT-API] Fetching metadata for {youtube_id}")

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 429:
        raise ValueError("RapidAPI rate limit exceeded. Check your subscription quota.")
    if resp.status_code != 200:
        raise ValueError(f"YT-API returned HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    if data.get("status") == "fail" or data.get("error"):
        err_msg = data.get("error") or data.get("message") or "Unknown API error"
        raise ValueError(f"YT-API error for {youtube_id}: {err_msg}")

    return data


def _cobalt_request(youtube_url: str, **kwargs) -> dict:
    """
    POST to the self-hosted Cobalt API. Cobalt downloads from YouTube on
    its own server and returns a tunnel URL that proxies the file content
    through Cobalt — no IP-restriction issues.

    kwargs are merged into the JSON body (downloadMode, videoQuality, etc).
    """
    body = {
        "url": youtube_url,
        "youtubeVideoCodec": "h264",
        **kwargs,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # If Cobalt requires auth, pass it
    cobalt_api_key = os.environ.get("COBALT_API_KEY", "")
    if cobalt_api_key:
        headers["Authorization"] = f"Api-Key {cobalt_api_key}"

    logger.info(f"[Cobalt] POST {COBALT_API_URL} body={body}")

    resp = requests.post(COBALT_API_URL, json=body, headers=headers, timeout=60)

    if resp.status_code == 429:
        raise ValueError("Cobalt rate limit exceeded. Try again later.")
    if resp.status_code != 200:
        raise ValueError(f"Cobalt returned HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()

    status = data.get("status")
    if status == "error":
        err = data.get("error", {})
        code = err.get("code", "unknown") if isinstance(err, dict) else str(err)
        raise ValueError(f"Cobalt error: {code}")

    return data


def _stream_download(url: str, dest_path: str, timeout: int = 600) -> str:
    """
    Download a URL to *dest_path* using chunked streaming.
    Works with both Cobalt tunnel URLs and regular CDN URLs.
    """
    logger.info(f"[Download] Streaming to {dest_path}")

    resp = requests.get(url, stream=True, timeout=timeout)
    resp.raise_for_status()

    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
            if chunk:
                f.write(chunk)

    file_size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    logger.info(f"[Download] Saved {dest_path} ({file_size_mb:.1f} MB)")
    return dest_path


def _cobalt_download(youtube_url: str, dest_path: str, **kwargs) -> str:
    """
    Full pipeline: call Cobalt → get tunnel/redirect URL → stream to file.
    """
    data = _cobalt_request(youtube_url, **kwargs)
    status = data.get("status")

    if status in ("tunnel", "redirect"):
        tunnel_url = data.get("url")
        if not tunnel_url:
            raise ValueError(f"Cobalt returned {status} but no URL")
        return _stream_download(tunnel_url, dest_path)

    elif status == "picker":
        # Multiple items — pick the first video/audio
        items = data.get("picker", [])
        if not items:
            raise ValueError("Cobalt picker returned no items")
        tunnel_url = items[0].get("url")
        if not tunnel_url:
            raise ValueError("Cobalt picker item has no URL")
        return _stream_download(tunnel_url, dest_path)

    elif status == "local-processing":
        # Cobalt wants us to merge locally — download tunnel files and merge
        tunnels = data.get("tunnel", [])
        proc_type = data.get("type", "")

        if len(tunnels) == 1 or proc_type == "audio":
            return _stream_download(tunnels[0], dest_path)

        if len(tunnels) >= 2 and proc_type == "merge":
            # Download video and audio separately, then merge
            output_dir = os.path.dirname(dest_path)
            video_tmp = os.path.join(output_dir, "_v.tmp")
            audio_tmp = os.path.join(output_dir, "_a.tmp")

            _stream_download(tunnels[0], video_tmp)
            _stream_download(tunnels[1], audio_tmp)

            _merge_av(video_tmp, audio_tmp, dest_path)

            os.remove(video_tmp)
            os.remove(audio_tmp)
            return dest_path

        # Fallback: just download first tunnel
        if tunnels:
            return _stream_download(tunnels[0], dest_path)

        raise ValueError(f"Cobalt local-processing has no tunnel URLs")

    else:
        raise ValueError(f"Unexpected Cobalt status: {status}")


def _merge_av(video_path: str, audio_path: str, output_path: str) -> str:
    """Mux separate video + audio files into a single mp4 with FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    logger.info(f"[FFmpeg] Merging video+audio -> {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg merge failed: {result.stderr[-500:]}")
    return output_path


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
    data = _fetch_yt_api_metadata(youtube_id)

    # Parse duration — API may return seconds (int) or ISO 8601 string
    raw_duration = data.get("lengthSeconds") or data.get("duration", 0)
    if isinstance(raw_duration, str):
        try:
            raw_duration = int(raw_duration)
        except ValueError:
            raw_duration = 0
    duration = int(raw_duration)

    # Find the highest available video height across all formats
    all_formats = data.get("adaptiveFormats", []) + data.get("formats", [])
    max_height: int | None = None
    for fmt in all_formats:
        h = fmt.get("height")
        if h and isinstance(h, (int, float)) and int(h) > (max_height or 0):
            max_height = int(h)

    # Pick best thumbnail
    thumbnails = data.get("thumbnail", data.get("thumbnails", []))
    thumb_url = ""
    if isinstance(thumbnails, list) and thumbnails:
        best = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))
        thumb_url = best.get("url", "")
    elif isinstance(thumbnails, str):
        thumb_url = thumbnails

    return {
        "title": data.get("title", ""),
        "duration": duration,
        "thumbnail": thumb_url,
        "uploader": data.get("channelTitle", data.get("uploader", "")),
        "max_source_height": max_height,
    }


def download_audio(youtube_url: str, youtube_id: str) -> str:
    """Download audio only from a YouTube video. Returns the file path."""
    output_dir = tempfile.mkdtemp(prefix="clipper_")
    raw_path = os.path.join(output_dir, f"{youtube_id}.m4a")

    try:
        _cobalt_download(
            youtube_url,
            raw_path,
            downloadMode="audio",
            audioFormat="best",
        )
    except Exception as e:
        raise ValueError(f"Failed to download audio: {e}")

    # Ensure output is m4a (AAC) for Whisper compatibility
    # Cobalt may return opus/webm; convert if needed
    m4a_path = os.path.join(output_dir, f"{youtube_id}_final.m4a")

    if raw_path.endswith(".m4a") and os.path.exists(raw_path):
        # Check if it's actually AAC by probing
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", raw_path],
            capture_output=True, text=True, timeout=30,
        )
        if "aac" in probe.stdout.lower() or "mp4a" in probe.stdout.lower():
            logger.info(f"[Audio] Downloaded AAC audio: {raw_path}")
            return raw_path

    # Convert to m4a/AAC
    cmd = [
        "ffmpeg", "-y", "-i", raw_path,
        "-vn", "-acodec", "aac", "-b:a", "128k",
        m4a_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        # If conversion fails, return raw file (Whisper can handle many formats)
        logger.warning(f"[Audio] FFmpeg conversion failed, using raw: {result.stderr[-200:]}")
        return raw_path

    os.remove(raw_path)
    logger.info(f"[Audio] Converted to AAC: {m4a_path}")
    return m4a_path


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
    QUALITY_MAP = {
        "best": "max",
        "480p": "480",
        "720p": "720",
        "1080p": "1080",
        "1440p": "1440",
        "2160p": "2160",
    }
    cobalt_quality = QUALITY_MAP.get(quality, "1080")

    output_dir = tempfile.mkdtemp(prefix="clipper_segment_")
    full_path = os.path.join(output_dir, "full.mp4")
    output_path = os.path.join(output_dir, "segment.mp4")

    try:
        _cobalt_download(
            youtube_url,
            full_path,
            downloadMode="auto",
            videoQuality=cobalt_quality,
        )
    except Exception as e:
        raise ValueError(f"Failed to download video segment: {e}")

    # Cut segment with FFmpeg
    logger.info(
        f"[Segment] Cutting {start_time:.1f}-{end_time:.1f}s from {quality}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time),
        "-to", str(end_time),
        "-i", full_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg segment cut failed: {result.stderr[-300:]}")

    os.remove(full_path)

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
    QUALITY_MAP = {
        "best": "max",
        "480p": "480",
        "720p": "720",
        "1080p": "1080",
        "1440p": "1440",
        "2160p": "2160",
    }
    cobalt_quality = QUALITY_MAP.get(quality, "1080")

    output_dir = tempfile.mkdtemp(prefix="clipper_full_")
    output_path = os.path.join(output_dir, f"{youtube_id}.mp4")

    logger.info(f"[Cache] Downloading full video: {youtube_id} at quality={quality}")

    try:
        _cobalt_download(
            youtube_url,
            output_path,
            downloadMode="auto",
            videoQuality=cobalt_quality,
        )
    except Exception as e:
        raise ValueError(f"Failed to download full video: {e}")

    if os.path.exists(output_path):
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(f"[Cache] Downloaded full video: {output_path} ({file_size_mb:.1f} MB)")
        return output_path

    # Fallback: find any video file in the directory
    for f in os.listdir(output_dir):
        if f.endswith(".mp4"):
            full_path = os.path.join(output_dir, f)
            logger.info(f"[Cache] Found video file: {full_path}")
            return full_path

    raise FileNotFoundError(f"Video file not found after download in {output_dir}")
