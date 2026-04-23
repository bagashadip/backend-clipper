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
        # ios/android clients expose full DASH format lists;
        # tv is kept as last resort for age-gated content.
        # "extractor_args": {"youtube": {"player_client": ["ios", "android", "web", "tv"]}},
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

    QUALITY_FORMATS = {
        "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "1440p": "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
        "2160p": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    }
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["480p"])

    # Format time as HH:MM:SS
    start_str = _seconds_to_timestamp(start_time)
    end_str = _seconds_to_timestamp(end_time)

    ydl_opts = _get_base_opts()
    ydl_opts.update({
        "format": fmt,
        "outtmpl": output_path,
        "download_ranges": yt_dlp.utils.download_range_func(
            None, [(start_time, end_time)]
        ),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
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
