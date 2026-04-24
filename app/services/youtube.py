import os
import logging
import tempfile
import yt_dlp

logger = logging.getLogger(__name__)

COOKIES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "cookies.txt")
DENO_PATH = os.path.expanduser("~/.deno/bin")

# Ensure deno is in PATH for yt-dlp JS challenge solving
if os.path.isdir(DENO_PATH) and DENO_PATH not in os.environ.get("PATH", ""):
    os.environ["PATH"] = DENO_PATH + ":" + os.environ.get("PATH", "")


def _get_base_opts() -> dict:
    opts = {
        "quiet": False,
        "no_warnings": True,
        # ios is the most reliable client on datacenter/server IPs — it always
        # exposes full DASH adaptive format lists regardless of IP reputation.
        # android and web are fallbacks; tv handles age-gated content.
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "tv", "web"]}}
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        logger.info(f"Using cookies from {COOKIES_FILE}")
    return opts


def check_video_duration(youtube_url: str) -> int:
    """Extract video duration in seconds WITHOUT downloading."""
    metadata = extract_metadata(youtube_url)
    duration = metadata.get("duration", 0)
    if not duration or duration <= 0:
        raise ValueError("Could not determine video duration")
    return int(duration)


def extract_metadata(youtube_url: str) -> dict:
    """Extract video metadata without downloading."""
    ydl_opts = _get_base_opts()
    ydl_opts["skip_download"] = True
    ydl_opts["ignore_no_formats_error"] = True
    ydl_opts["format"] = "best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            return {
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
                "uploader": info.get("uploader", ""),
            }
    except yt_dlp.utils.DownloadError as e:
        raise ValueError(f"Invalid YouTube URL or video unavailable: {e}")


def download_audio(youtube_url: str, youtube_id: str) -> str:
    """Download audio only from a YouTube video. Returns the file path."""
    output_dir = tempfile.mkdtemp(prefix="clipper_")
    # Don't include extension in template — FFmpegExtractAudio postprocessor adds it
    output_template = os.path.join(output_dir, youtube_id)

    ydl_opts = _get_base_opts()
    ydl_opts.update({
        # "format": "140/bestaudio[ext=m4a]/bestaudio/best",
        "format": "ba/b",
        "outtmpl": output_template + ".%(ext)s",
        "extract_flat": False,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
        }],
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        # After FFmpegExtractAudio, the file should be {id}.m4a
        expected_path = output_template + ".m4a"
        if os.path.exists(expected_path):
            return expected_path

        # Fallback: find any audio file in the directory
        for f in os.listdir(output_dir):
            if f.startswith(youtube_id):
                return os.path.join(output_dir, f)

        raise FileNotFoundError(f"Audio file not found after download in {output_dir}")

    except yt_dlp.utils.DownloadError as e:
        raise ValueError(f"Failed to download audio: {e}")


def download_video_segment(youtube_url: str, start_time: float, end_time: float, quality: str = "480p") -> str:
    """Download only a specific segment of the video using yt-dlp --download-sections."""
    output_dir = tempfile.mkdtemp(prefix="clipper_segment_")
    output_path = os.path.join(output_dir, "segment.mp4")

    # Prefer H264 (avc1) video + M4A audio so output is always true H264/MP4.
    # YouTube provides H264 up to 1080p; 1440p/2160p tiers fall through to VP9
    # but the postprocessor will recode those to H264 anyway.
    # Never fall back to `best` (muxed progressive) — YouTube caps those at 720p.
    # Always stay on adaptive (bestvideo+bestaudio) chains to get real resolution.
    # H264 (avc1) is preferred; VP9/AV1 are acceptable since video_renderer.py
    # re-encodes to H264 anyway.
    QUALITY_FORMATS = {
        "best":  (
            "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[vcodec^=avc1]+bestaudio"
            "/bestvideo+bestaudio"
        ),
        "480p":  (
            "bestvideo[height<=480][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[height<=480][vcodec^=avc1]+bestaudio"
            "/bestvideo[height<=480]+bestaudio"
        ),
        "720p":  (
            "bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[height<=720][vcodec^=avc1]+bestaudio"
            "/bestvideo[height<=720]+bestaudio"
        ),
        "1080p": (
            "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[height<=1080][vcodec^=avc1]+bestaudio"
            "/bestvideo[height<=1080]+bestaudio"
        ),
        "1440p": (
            "bestvideo[height<=1440][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[height<=1440]+bestaudio"
        ),
        "2160p": (
            "bestvideo[height<=2160][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[height<=2160]+bestaudio"
        ),
    }
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])

    # Log the format string being requested
    logger.info(f"[yt-dlp] Requested quality={quality!r}, format string: {fmt}")

    def _log_format(d: dict) -> None:
        """Progress hook: log the actual format yt-dlp selected on first download start."""
        if d.get("status") != "downloading":
            return
        info = d.get("info_dict", {})
        # Only log once per stream (avoid spam on each chunk)
        if info.get("_format_logged"):
            return
        info["_format_logged"] = True

        fmt_id   = info.get("format_id", "?")
        vcodec   = info.get("vcodec", "?")
        acodec   = info.get("acodec", "?")
        height   = info.get("height", "?")
        width    = info.get("width", "?")
        ext      = info.get("ext", "?")
        filesize = info.get("filesize") or info.get("filesize_approx")
        size_mb  = f"{filesize / 1024 / 1024:.1f} MB" if filesize else "unknown size"

        logger.info(
            f"[yt-dlp] Downloading format_id={fmt_id!r} | "
            f"{width}x{height} | vcodec={vcodec} | acodec={acodec} | "
            f"ext={ext} | {size_mb}"
        )

    # Format time as HH:MM:SS
    start_str = _seconds_to_timestamp(start_time)
    end_str = _seconds_to_timestamp(end_time)

    ydl_opts = _get_base_opts()
    ydl_opts.update({
        "format": fmt,
        # Prefer H264 > VP9 > AV1, and MP4/M4A containers
        "format_sort": ["vcodec:h264", "acodec:m4a", "ext:mp4"],
        "outtmpl": output_path,
        "download_ranges": yt_dlp.utils.download_range_func(
            None, [(start_time, end_time)]
        ),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "progress_hooks": [_log_format],
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        if os.path.exists(output_path):
            return output_path

        # Check for merged output
        for f in os.listdir(output_dir):
            if f.endswith(".mp4"):
                return os.path.join(output_dir, f)

        raise FileNotFoundError("Segment file not found after download")

    except yt_dlp.utils.DownloadError as e:
        raise ValueError(f"Failed to download video segment: {e}")


def _seconds_to_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
