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
        # mweb uses a different API endpoint that often bypasses datacenter IP
        # restrictions; android gives DASH adaptive streams; ios gives HLS;
        # tv handles age-gated content; web as last resort.
        # "extractor_args": {"youtube": {"player_client": ["mweb", "android", "ios", "tv", "web"]}}
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
        # bestaudio[ext=m4a]: prefer native m4a (no re-encode needed)
        # bestaudio: any audio-only stream
        # best: muxed stream as last resort — FFmpegExtractAudio will strip the video
        "format": "best",
        # "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
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
    """Download only a specific segment of the video using yt-dlp --download-sections.

    NOTE: download_ranges is currently DISABLED for diagnostic purposes.
    Theory: FFmpeg on GCP fails to cut DASH streams mid-stream, causing yt-dlp
    to silently fall back to a low-res muxed format. Without download_ranges,
    yt-dlp downloads the full video at the correct resolution — MoviePy then
    trims the segment. Confirm by checking the [yt-dlp] log lines for resolution.
    """
    output_dir = tempfile.mkdtemp(prefix="clipper_segment_")
    output_path = os.path.join(output_dir, "segment.mp4")

    # Map quality string → target height for format_sort res: hint
    QUALITY_HEIGHT = {
        "best": None,  # no height preference, just take highest
        "480p": 480,
        "720p": 720,
        "1080p": 1080,
        "1440p": 1440,
        "2160p": 2160,
    }
    target_height = QUALITY_HEIGHT.get(quality)

    fmt = "bestvideo+bestaudio/best"
    fmt_sort = ["res:" + str(target_height)] if target_height else ["res:2160"]

    logger.info(
        f"[yt-dlp] Requested quality={quality!r} "
        f"(target_height={target_height}), format={fmt!r}, "
        f"format_sort={fmt_sort}"
    )

    # Pre-download: list all formats YouTube is offering from this IP.
    # If only format 18 (360p) appears, cookies are expired or IP is restricted.
    _debug_opts = _get_base_opts()
    _debug_opts["quiet"] = True
    _debug_opts["no_warnings"] = True
    try:
        with yt_dlp.YoutubeDL(_debug_opts) as _ydl:
            _info = _ydl.extract_info(youtube_url, download=False)
            if _info:
                _fmts = [
                    (f.get("format_id"), f.get("height"), (f.get("vcodec") or "")[:12], f.get("ext"))
                    for f in (_info.get("formats") or [])
                ]
                _fmts.sort(key=lambda x: x[1] or 0, reverse=True)
                logger.info(f"[yt-dlp] Formats available from this IP (top 15): {_fmts[:15]}")
    except Exception as _e:
        logger.warning(f"[yt-dlp] Format listing failed: {_e}")

    def _log_format(d: dict) -> None:
        """Progress hook: log the actual format yt-dlp selected on first download start."""
        if d.get("status") != "downloading":
            return
        info = d.get("info_dict", {})
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

    ydl_opts = _get_base_opts()
    ydl_opts.update({
        "format": fmt,
        # res:N — prefer resolution closest to N (e.g. res:1080 picks 1080p,
        # res:2160 picks the highest available resolution).
        # NOTE: +res would be ASCENDING (lowest first) — do NOT use it.
        "format_sort": fmt_sort,
        "outtmpl": output_path,
        # ── DIAGNOSTIC: download_ranges disabled ──────────────────────────
        # download_ranges cuts the stream mid-download via FFmpeg. On GCP this
        # appears to cause DASH segment fetch failures, making yt-dlp silently
        # fall back to a low-res muxed format. Disabled until confirmed fixed.
        # Once confirmed, re-enable and trim will happen server-side:
        #   "download_ranges": yt_dlp.utils.download_range_func(
        #       None, [(start_time, end_time)]
        #   ),
        # ──────────────────────────────────────────────────────────────────
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
