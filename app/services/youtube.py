import os
import time
import logging
import tempfile
import yt_dlp

logger = logging.getLogger(__name__)

COOKIES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "cookies.txt")
DENO_PATH = os.path.expanduser("~/.deno/bin")

# Ensure deno is in PATH for yt-dlp JS challenge solving
if os.path.isdir(DENO_PATH) and DENO_PATH not in os.environ.get("PATH", ""):
    os.environ["PATH"] = DENO_PATH + ":" + os.environ.get("PATH", "")

# URL for the bgutil PO Token HTTP server sidecar.
# Defaults to localhost for bare-metal / dev; overridden to the Docker service
# name via the BGUTIL_POT_SERVER_URL env var in docker-compose.yml.
BGUTIL_POT_SERVER_URL = os.environ.get("BGUTIL_POT_SERVER_URL", "http://127.0.0.1:4416")


def _get_base_opts() -> dict:
    opts = {
        "quiet": False,
        "no_warnings": True,
        "verbose": True, # INI WAJIB ADA BUAT DEBUGGING TOKEN
        "extractor_args": {
            "youtube": {
                # Priority order:
                # 1. mweb:       bgutil provides GVS PO tokens; HLS streams bypass SABR
                # 2. android_vr: NO PO token required; NOT SABR-enforced; DASH available
                # 3. tv:         NO PO token required; sometimes SABR "in some cases"
                # 4. web:        bgutil provides GVS PO tokens; SABR-enforced (last resort)
                #
                # web_safari removed: SABR is now enforced for web_safari too (see #12482)
                # android_vr is the key new addition — bypasses both PO token AND SABR
                "player_client": ["mweb", "android_vr", "tv", "web"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": [BGUTIL_POT_SERVER_URL],
            },
        },
    }
    if os.path.exists(COOKIES_FILE):
        age_days = (time.time() - os.path.getmtime(COOKIES_FILE)) / 86400
        if age_days <= 7:
            opts["cookiefile"] = COOKIES_FILE
            logger.info(f"Using cookies from {COOKIES_FILE} (age: {age_days:.1f}d)")
        else:
            logger.warning(
                f"Skipping stale cookies.txt (age: {age_days:.1f}d > 7d). "
                "Expired cookies cause LOGIN_REQUIRED for all clients. "
                "Replace cookies.txt with a fresh export."
            )
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

            # Find the highest available video height across all formats
            max_height: int | None = None
            for fmt in info.get("formats", []):
                h = fmt.get("height")
                if h and isinstance(h, (int, float)) and int(h) > (max_height or 0):
                    max_height = int(h)

            return {
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
                "uploader": info.get("uploader", ""),
                "max_source_height": max_height,
            }
    except yt_dlp.utils.DownloadError as e:
        raise ValueError(f"Invalid YouTube URL or video unavailable: {e}")


def download_audio(youtube_url: str, youtube_id: str) -> str:
    """Download audio only from a YouTube video. Returns the file path."""
    output_dir = tempfile.mkdtemp(prefix="clipper_")
    # Don't include extension in template — FFmpegExtractAudio postprocessor adds it
    output_template = os.path.join(output_dir, youtube_id)

    ydl_opts = _get_base_opts()
    ydl_opts["extractor_args"]["youtube"]["player_client"] = ["android", "ios"]
    ydl_opts.update({
        # Fleksibel: Cari m4a murni. Kalau gak ada, ambil best audio.
        # Kalau best audio disembunyiin juga, ambil best (mp4 gabungan).
        "format": "m4a/bestaudio/best",
        
        # HAPUS BARIS "format_sort" TOTAL DARI SINI
        
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


def download_video_segment(youtube_url: str, start_time: float, end_time: float, quality: str = "1080p") -> str:
    """Download only a specific segment of the video using yt-dlp."""
    output_dir = tempfile.mkdtemp(prefix="clipper_segment_")
    output_path = os.path.join(output_dir, "segment.mp4")

    QUALITY_HEIGHT = {
        "best": None,
        "480p": 480,
        "720p": 720,
        "1080p": 1080,
        "1440p": 1440,
        "2160p": 2160,
    }
    target_height = QUALITY_HEIGHT.get(quality)

    if target_height:
        # 1st: separate DASH bestvideo+bestaudio (best quality, needs two streams)
        # 2nd: best combined format (HLS from mweb/web_safari) at or below target height
        # 3rd: absolute best combined format regardless of height (last resort)
        fmt = f"bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]/best"
    else:
        fmt = "bestvideo+bestaudio/best"
        
    fmt_sort = ["res:" + str(target_height)] if target_height else ["res:2160"]

    logger.info(
        f"[yt-dlp] Requested quality={quality!r} "
        f"(target_height={target_height}), format={fmt!r}, "
        f"format_sort={fmt_sort}"
    )

    def _log_format(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        info = d.get("info_dict", {})
        if info.get("_format_logged"):
            return
        info["_format_logged"] = True

        fmt_id   = info.get("format_id", "?")
        height   = info.get("height", "?")
        width    = info.get("width", "?")
        logger.info(f"[yt-dlp] Downloading format_id={fmt_id!r} | {width}x{height}")

    ydl_opts = _get_base_opts()
    # Do NOT override player_client here — _get_base_opts() already sets
    # ["mweb", "web"] which bgutil supplies GVS PO tokens for automatically.
    # tv/ios/android are NOT supported by the bgutil provider and fall back
    # to format 18 (360p muxed) on datacenter IPs.

    ydl_opts.update({
        "format": fmt,
        "format_sort": fmt_sort,
        "outtmpl": output_path,
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "progress_hooks": [_log_format],
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        if os.path.exists(output_path):
            return output_path

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
