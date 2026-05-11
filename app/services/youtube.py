import os
import subprocess
import logging
import tempfile
import re
import requests

logger = logging.getLogger(__name__)

# ── RapidAPI configuration ─────────────────────────────────────────────────
# YouTube Media Downloader by DataFanatic — provides both metadata and
# proxied download URLs through a single endpoint.
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "youtube-media-downloader.p.rapidapi.com"
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"

# Chunk size for streaming downloads (1 MB)
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024

# Retry configuration for transient download failures
_DOWNLOAD_MAX_RETRIES = 3


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
    """Return the RapidAPI request headers."""
    if not RAPIDAPI_KEY:
        raise ValueError(
            "RAPIDAPI_KEY environment variable is not set. "
            "Add it to your .env / docker-compose environment."
        )
    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }


def _fetch_video_data(youtube_id: str) -> dict:
    """
    Call YouTube Media Downloader API for full video data including
    metadata, download URLs (videos + audios), thumbnails, and subtitles.

    This single call replaces both the old YT-API metadata call and
    the Cobalt download request.
    """
    url = f"{RAPIDAPI_BASE}/v2/video/details"
    params = {"videoId": youtube_id}
    headers = _get_rapidapi_headers()

    logger.info(f"[YT-DL API] Fetching video data for {youtube_id}")

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 429:
        raise ValueError("RapidAPI rate limit exceeded. Check your subscription quota.")
    if resp.status_code != 200:
        raise ValueError(f"YT-DL API returned HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()

    # Check for API-level errors
    error_id = data.get("errorId", "")
    if error_id and error_id not in ("Success", "None", "0", "", None):
        raise ValueError(f"YT-DL API error for {youtube_id}: errorId={error_id}")

    return data


def _pick_video_url(data: dict, quality: str = "1080p") -> tuple[str, bool]:
    """
    Select the best video download URL from the API response.

    Preference order:
      1. MP4 with audio at the requested quality (no merge needed)
      2. MP4 video-only at the requested quality (needs audio merge)
      3. Any format at the requested quality
      4. Best available below the requested quality

    Returns:
        (url, has_audio) — the download URL and whether it includes audio.
    """
    QUALITY_ORDER = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]

    videos_data = data.get("videos", {})
    items = videos_data.get("items", [])

    if not items:
        raise ValueError("No video streams available from API")

    # Parse target quality height
    target_height = int(quality.replace("p", "")) if quality != "best" else 99999

    # Group by quality
    by_quality: dict[str, list] = {}
    for item in items:
        q = item.get("quality", "")
        by_quality.setdefault(q, []).append(item)

    # Try to find exact match first
    target_q = f"{target_height}p" if quality != "best" else None

    def _find_at_quality(q_label: str) -> tuple[str, bool] | None:
        candidates = by_quality.get(q_label, [])
        # Prefer MP4 with audio
        for c in candidates:
            if c.get("extension") == "mp4" and c.get("hasAudio"):
                return (c["url"], True)
        # Then MP4 video-only
        for c in candidates:
            if c.get("extension") == "mp4" and not c.get("hasAudio"):
                return (c["url"], False)
        # Then any format
        for c in candidates:
            if c.get("url"):
                return (c["url"], bool(c.get("hasAudio")))
        return None

    if quality == "best":
        # Try highest quality first
        for q in QUALITY_ORDER:
            result = _find_at_quality(q)
            if result:
                logger.info(f"[Pick Video] Selected {q} (hasAudio={result[1]})")
                return result
    else:
        # Try exact match
        result = _find_at_quality(target_q)
        if result:
            logger.info(f"[Pick Video] Selected exact {target_q} (hasAudio={result[1]})")
            return result

        # Try lower qualities as fallback
        idx = QUALITY_ORDER.index(target_q) if target_q in QUALITY_ORDER else 0
        for q in QUALITY_ORDER[idx:]:
            result = _find_at_quality(q)
            if result:
                logger.info(f"[Pick Video] Fallback to {q} (hasAudio={result[1]})")
                return result

    # Last resort: first item with a URL
    for item in items:
        if item.get("url"):
            return (item["url"], bool(item.get("hasAudio")))

    raise ValueError("No downloadable video stream found")


def _pick_audio_url(data: dict) -> str:
    """
    Select the best audio download URL from the API response.
    Prefers m4a (AAC) for Whisper compatibility, then any available.
    """
    audios_data = data.get("audios", {})
    items = audios_data.get("items", [])

    if not items:
        raise ValueError("No audio streams available from API")

    # Prefer m4a (AAC)
    for item in items:
        if item.get("extension") == "m4a" and item.get("url"):
            logger.info("[Pick Audio] Selected m4a stream")
            return item["url"]

    # Fallback: any audio with a URL
    for item in items:
        if item.get("url"):
            logger.info(f"[Pick Audio] Selected {item.get('extension', '?')} stream")
            return item["url"]

    raise ValueError("No downloadable audio stream found")


def _stream_download(url: str, dest_path: str, timeout: int = 600) -> str:
    """
    Download a URL to *dest_path* using chunked streaming with retry logic.
    """
    last_error = None

    for attempt in range(1, _DOWNLOAD_MAX_RETRIES + 1):
        try:
            logger.info(f"[Download] Streaming to {dest_path} (attempt {attempt})")

            resp = requests.get(url, stream=True, timeout=timeout)
            resp.raise_for_status()

            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)

            file_size_mb = os.path.getsize(dest_path) / (1024 * 1024)
            logger.info(f"[Download] Saved {dest_path} ({file_size_mb:.1f} MB)")
            return dest_path

        except Exception as e:
            last_error = e
            logger.warning(f"[Download] Attempt {attempt} failed: {e}")
            if attempt < _DOWNLOAD_MAX_RETRIES:
                import time
                time.sleep(2 * attempt)

    raise ValueError(
        f"Failed to download after {_DOWNLOAD_MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )


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


def _download_video_with_audio(
    data: dict,
    dest_path: str,
    quality: str = "1080p",
) -> str:
    """
    Download video from the API response data. If the selected stream
    has no audio, automatically downloads audio separately and merges.

    Args:
        data: Full API response from _fetch_video_data()
        dest_path: Where to save the final mp4
        quality: Target quality string

    Returns:
        Path to the downloaded (and possibly merged) video file
    """
    video_url, has_audio = _pick_video_url(data, quality)

    if has_audio:
        # Stream already has audio — download directly
        return _stream_download(video_url, dest_path)

    # Video-only: need to download audio separately and merge
    output_dir = os.path.dirname(dest_path)
    video_tmp = os.path.join(output_dir, "_video_tmp.mp4")
    audio_tmp = os.path.join(output_dir, "_audio_tmp.m4a")

    try:
        audio_url = _pick_audio_url(data)

        _stream_download(video_url, video_tmp)
        _stream_download(audio_url, audio_tmp)

        _merge_av(video_tmp, audio_tmp, dest_path)

        return dest_path
    finally:
        # Clean up temp files
        for tmp in [video_tmp, audio_tmp]:
            if os.path.exists(tmp):
                os.remove(tmp)


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
    data = _fetch_video_data(youtube_id)

    # Parse duration
    raw_duration = data.get("lengthSeconds", 0)
    if isinstance(raw_duration, str):
        try:
            raw_duration = int(raw_duration)
        except ValueError:
            raw_duration = 0
    duration = int(raw_duration)

    # Find the highest available video height
    videos_data = data.get("videos", {})
    video_items = videos_data.get("items", []) if isinstance(videos_data, dict) else []
    max_height: int | None = None
    for item in video_items:
        q = item.get("quality", "")
        # Parse "1080p" -> 1080
        match = re.match(r"(\d+)p", q)
        if match:
            h = int(match.group(1))
            if h > (max_height or 0):
                max_height = h

    # Pick best thumbnail
    thumbnails = data.get("thumbnails", [])
    thumb_url = ""
    if isinstance(thumbnails, list) and thumbnails:
        best = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))
        thumb_url = best.get("url", "")
    elif isinstance(thumbnails, str):
        thumb_url = thumbnails

    # Channel info
    channel = data.get("channel", {})
    uploader = ""
    if isinstance(channel, dict):
        uploader = channel.get("name", "")
    elif isinstance(channel, str):
        uploader = channel

    return {
        "title": data.get("title", ""),
        "duration": duration,
        "thumbnail": thumb_url,
        "uploader": uploader,
        "max_source_height": max_height,
    }


def download_audio(youtube_url: str, youtube_id: str) -> str:
    """Download audio only from a YouTube video. Returns the file path."""
    output_dir = tempfile.mkdtemp(prefix="clipper_")
    raw_path = os.path.join(output_dir, f"{youtube_id}.m4a")

    try:
        data = _fetch_video_data(youtube_id)
        audio_url = _pick_audio_url(data)
        _stream_download(audio_url, raw_path)
    except Exception as e:
        raise ValueError(f"Failed to download audio: {e}")

    # Ensure output is m4a (AAC) for Whisper compatibility
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
    youtube_id = get_youtube_video_id(youtube_url)

    output_dir = tempfile.mkdtemp(prefix="clipper_segment_")
    full_path = os.path.join(output_dir, "full.mp4")
    output_path = os.path.join(output_dir, "segment.mp4")

    try:
        data = _fetch_video_data(youtube_id)
        _download_video_with_audio(data, full_path, quality)
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
    output_dir = tempfile.mkdtemp(prefix="clipper_full_")
    output_path = os.path.join(output_dir, f"{youtube_id}.mp4")

    logger.info(f"[Cache] Downloading full video: {youtube_id} at quality={quality}")

    try:
        data = _fetch_video_data(youtube_id)
        _download_video_with_audio(data, output_path, quality)
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
