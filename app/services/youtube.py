import os
import subprocess
import logging
import tempfile
import re
import requests

logger = logging.getLogger(__name__)

# ── RapidAPI YT-API configuration ──────────────────────────────────────────
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "yt-api.p.rapidapi.com"
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"

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

def _get_headers() -> dict:
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


def _fetch_yt_api_data(youtube_id: str) -> dict:
    """
    Single master call to YT-API.  Returns the full JSON payload containing
    metadata (title, duration, thumbnails, channelTitle) **and** an array of
    downloadable format links.

    Raises ValueError on API errors so callers can surface a clean message.
    """
    url = f"{RAPIDAPI_BASE}/dl"
    params = {"id": youtube_id}
    headers = _get_headers()

    logger.info(f"[YT-API] Fetching data for {youtube_id}")

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 429:
        raise ValueError(
            "RapidAPI rate limit exceeded. Check your subscription quota."
        )
    if resp.status_code != 200:
        raise ValueError(
            f"YT-API returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    data = resp.json()

    # Handle API-level errors
    if data.get("status") == "fail" or data.get("error"):
        err_msg = data.get("error") or data.get("message") or "Unknown API error"
        raise ValueError(f"YT-API error for {youtube_id}: {err_msg}")

    return data


def _stream_download(url: str, dest_path: str, timeout: int = 600) -> str:
    """
    Download a remote URL to *dest_path* using chunked streaming.
    Returns the absolute path on success.
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


def _pick_best_audio(formats: list[dict]) -> dict | None:
    """
    From the API format list, find the best audio-only stream.
    Prefers m4a/mp4a codecs, then falls back to any audio.
    """
    audio_formats = [
        f for f in formats
        if f.get("mimeType", "").startswith("audio/")
        and f.get("url")
    ]
    if not audio_formats:
        return None

    # Prefer m4a (AAC) > webm (opus)
    m4a = [f for f in audio_formats if "mp4a" in f.get("mimeType", "") or "m4a" in f.get("mimeType", "")]
    if m4a:
        # Sort by bitrate descending
        m4a.sort(key=lambda f: f.get("bitrate", 0), reverse=True)
        return m4a[0]

    # Fallback: highest bitrate audio
    audio_formats.sort(key=lambda f: f.get("bitrate", 0), reverse=True)
    return audio_formats[0]


def _pick_best_video(
    formats: list[dict],
    target_height: int | None = None,
    allow_audio: bool = True,
) -> dict | None:
    """
    From the API format list, pick the best video stream.

    If *allow_audio* is True, combined (muxed) formats are included.
    *target_height* caps the max resolution; None = best available.
    """
    video_formats = [
        f for f in formats
        if f.get("url")
        and (
            f.get("mimeType", "").startswith("video/")
            or (allow_audio and "video" in f.get("mimeType", ""))
        )
        and f.get("height")
    ]
    if not video_formats:
        return None

    if target_height:
        # Filter to at most target_height
        capped = [f for f in video_formats if f["height"] <= target_height]
        pool = capped if capped else video_formats
    else:
        pool = video_formats

    # Sort: resolution desc, then bitrate desc
    pool.sort(key=lambda f: (f.get("height", 0), f.get("bitrate", 0)), reverse=True)
    return pool[0]


def _pick_best_combined(
    formats: list[dict],
    target_height: int | None = None,
) -> dict | None:
    """
    Pick the best combined (video+audio) mp4 format from adaptiveFormats
    or formats list. These are single-file downloads that don't need merge.
    """
    combined = [
        f for f in formats
        if f.get("url")
        and f.get("height")
        and f.get("mimeType", "").startswith("video/mp4")
        and f.get("hasAudio", False)
    ]
    if not combined:
        # Fallback: look for any format with both audio and video
        combined = [
            f for f in formats
            if f.get("url")
            and f.get("height")
            and f.get("hasAudio", False)
        ]
    if not combined:
        return None

    if target_height:
        capped = [f for f in combined if f["height"] <= target_height]
        pool = capped if capped else combined
    else:
        pool = combined

    pool.sort(key=lambda f: (f.get("height", 0), f.get("bitrate", 0)), reverse=True)
    return pool[0]


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


def _get_all_formats(data: dict) -> list[dict]:
    """
    Extract all available formats from the API response.
    The YT-API response may have formats at different nesting levels.
    """
    formats = []

    # adaptiveFormats are separate audio-only and video-only streams
    formats.extend(data.get("adaptiveFormats", []))

    # formats are combined (muxed) streams
    for fmt in data.get("formats", []):
        fmt["hasAudio"] = True  # combined formats have audio
        formats.append(fmt)

    return formats


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
    data = _fetch_yt_api_data(youtube_id)

    # Parse duration — API may return seconds (int) or ISO 8601 string
    raw_duration = data.get("lengthSeconds") or data.get("duration", 0)
    if isinstance(raw_duration, str):
        try:
            raw_duration = int(raw_duration)
        except ValueError:
            raw_duration = 0
    duration = int(raw_duration)

    # Find the highest available video height across all formats
    all_formats = _get_all_formats(data)
    max_height: int | None = None
    for fmt in all_formats:
        h = fmt.get("height")
        if h and isinstance(h, (int, float)) and int(h) > (max_height or 0):
            max_height = int(h)

    # Pick best thumbnail
    thumbnails = data.get("thumbnail", data.get("thumbnails", []))
    thumb_url = ""
    if isinstance(thumbnails, list) and thumbnails:
        # Pick largest thumbnail
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
    data = _fetch_yt_api_data(youtube_id)
    all_formats = _get_all_formats(data)

    audio_fmt = _pick_best_audio(all_formats)

    if audio_fmt and audio_fmt.get("url"):
        # Direct audio-only stream available
        output_dir = tempfile.mkdtemp(prefix="clipper_")
        ext = "m4a" if "mp4a" in audio_fmt.get("mimeType", "") else "webm"
        raw_path = os.path.join(output_dir, f"{youtube_id}.{ext}")

        _stream_download(audio_fmt["url"], raw_path)

        # Ensure output is m4a (AAC) for Whisper compatibility
        m4a_path = os.path.join(output_dir, f"{youtube_id}.m4a")
        if ext != "m4a":
            cmd = [
                "ffmpeg", "-y", "-i", raw_path,
                "-vn", "-acodec", "aac", "-b:a", "128k",
                m4a_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise ValueError(f"FFmpeg audio conversion failed: {result.stderr[-300:]}")
            os.remove(raw_path)
        else:
            m4a_path = raw_path

        logger.info(f"[Audio] Downloaded audio: {m4a_path}")
        return m4a_path

    # Fallback: download a combined (muxed) format and extract audio
    combined = _pick_best_combined(all_formats, target_height=480)
    if not combined or not combined.get("url"):
        raise ValueError(
            f"No audio or combined format available for {youtube_id}. "
            "The video may be unavailable or region-locked."
        )

    output_dir = tempfile.mkdtemp(prefix="clipper_")
    video_path = os.path.join(output_dir, f"{youtube_id}_tmp.mp4")
    m4a_path = os.path.join(output_dir, f"{youtube_id}.m4a")

    _stream_download(combined["url"], video_path)

    # Extract audio with FFmpeg
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "aac", "-b:a", "128k",
        m4a_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg audio extraction failed: {result.stderr[-300:]}")

    os.remove(video_path)
    logger.info(f"[Audio] Extracted audio from combined format: {m4a_path}")
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
    data = _fetch_yt_api_data(youtube_id)
    all_formats = _get_all_formats(data)

    QUALITY_HEIGHT = {
        "best": None,
        "480p": 480,
        "720p": 720,
        "1080p": 1080,
        "1440p": 1440,
        "2160p": 2160,
    }
    target_height = QUALITY_HEIGHT.get(quality)

    # For high-res, don't cap (get best available)
    cap = target_height if target_height and target_height < 1440 else None

    output_dir = tempfile.mkdtemp(prefix="clipper_segment_")
    output_path = os.path.join(output_dir, "segment.mp4")

    # Try combined format first (no merge needed)
    combined = _pick_best_combined(all_formats, target_height=cap)
    if combined and combined.get("url"):
        full_path = os.path.join(output_dir, "full.mp4")
        _stream_download(combined["url"], full_path)

        logger.info(
            f"[Segment] Cutting {start_time:.1f}-{end_time:.1f}s "
            f"from combined {combined.get('height', '?')}p"
        )

        # Cut segment with FFmpeg
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

    # Fallback: separate video + audio streams, merge, then cut
    video_fmt = _pick_best_video(all_formats, target_height=cap, allow_audio=False)
    audio_fmt = _pick_best_audio(all_formats)

    if not video_fmt or not video_fmt.get("url"):
        raise ValueError(f"No suitable video format found for {youtube_id} at {quality}")

    video_tmp = os.path.join(output_dir, "v_tmp")
    _stream_download(video_fmt["url"], video_tmp)

    if audio_fmt and audio_fmt.get("url"):
        audio_tmp = os.path.join(output_dir, "a_tmp")
        _stream_download(audio_fmt["url"], audio_tmp)
        merged = os.path.join(output_dir, "merged.mp4")
        _merge_av(video_tmp, audio_tmp, merged)
        os.remove(video_tmp)
        os.remove(audio_tmp)
        source = merged
    else:
        source = video_tmp

    logger.info(
        f"[Segment] Cutting {start_time:.1f}-{end_time:.1f}s "
        f"from {video_fmt.get('height', '?')}p"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time),
        "-to", str(end_time),
        "-i", source,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg segment cut failed: {result.stderr[-300:]}")

    os.remove(source)

    if os.path.exists(output_path):
        return output_path

    # Last resort: find any mp4 in the dir
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
    data = _fetch_yt_api_data(youtube_id)
    all_formats = _get_all_formats(data)

    QUALITY_HEIGHT = {
        "best": None,
        "480p": 480,
        "720p": 720,
        "1080p": 1080,
        "1440p": 1440,
        "2160p": 2160,
    }
    target_height = QUALITY_HEIGHT.get(quality)

    # For high-res, don't cap
    cap = target_height if target_height and target_height < 1440 else None

    output_dir = tempfile.mkdtemp(prefix="clipper_full_")
    output_path = os.path.join(output_dir, f"{youtube_id}.mp4")

    logger.info(f"[Cache] Downloading full video: {youtube_id} at quality={quality}")

    # Try combined format first (simpler, single file)
    combined = _pick_best_combined(all_formats, target_height=cap)
    if combined and combined.get("url"):
        _stream_download(combined["url"], output_path)
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(
            f"[Cache] Downloaded combined {combined.get('height', '?')}p: "
            f"{output_path} ({file_size_mb:.1f} MB)"
        )
        return output_path

    # Fallback: separate video + audio, merge with FFmpeg
    video_fmt = _pick_best_video(all_formats, target_height=cap, allow_audio=False)
    audio_fmt = _pick_best_audio(all_formats)

    if not video_fmt or not video_fmt.get("url"):
        raise ValueError(
            f"No suitable video format found for {youtube_id} at {quality}"
        )

    video_tmp = os.path.join(output_dir, f"{youtube_id}_v.tmp")
    _stream_download(video_fmt["url"], video_tmp)

    if audio_fmt and audio_fmt.get("url"):
        audio_tmp = os.path.join(output_dir, f"{youtube_id}_a.tmp")
        _stream_download(audio_fmt["url"], audio_tmp)
        _merge_av(video_tmp, audio_tmp, output_path)
        os.remove(video_tmp)
        os.remove(audio_tmp)
    else:
        # Video-only, just rename
        os.rename(video_tmp, output_path)

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
