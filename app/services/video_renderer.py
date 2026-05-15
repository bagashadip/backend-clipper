"""Video renderer — single-pass FFmpeg pipeline.

This module replaces the previous MoviePy + Pillow frame-by-frame pipeline
with a single-pass FFmpeg invocation that performs:

    [trim] → [crop to 9:16] → [resize] → [burn ASS subtitles] → [encode]

All stages are chained inside FFmpeg's filter graph so the video is decoded
and re-encoded only ONCE. This is dramatically faster than MoviePy and uses
far less memory.

Caption styling is unified through the ASS subtitle pipeline. The legacy
Pillow code path is removed; "classic", "hormozi", "hormozi_dynamic",
"impact_pop", "typewriter", "kenergy", "midnight", "cleanboss", "staccato",
and "serenity" all flow through a single ASS preset table.
"""

import json
import logging
import os
import subprocess
import tempfile

from app.services.youtube import download_video_segment
from app.services.subtitle_ass import generate_dynamic_ass

logger = logging.getLogger(__name__)

# ── Encoder configuration ────────────────────────────────────────────────
_VCODEC = "libx264"
_PRESET_FAST = "ultrafast"
_PRESET_BALANCED = "veryfast"  # Faster than 'medium' with minor quality cost
_PRESET_QUALITY = "medium"

# Threading: with celery concurrency=2, give each FFmpeg job half the cores
# to avoid oversubscription. Override with FFMPEG_THREADS env var if needed.
_FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "2")

logger.info("video_renderer: Using CPU encoding (libx264) with single-pass FFmpeg pipeline")


# ── Quality presets ──────────────────────────────────────────────────────

# Vertical (9:16) target resolutions and bitrates
VERTICAL_QUALITY: dict[str, dict] = {
    "480p":  {"width": 480,  "height": 854,  "bitrate": "2M",  "crf": 23},
    "720p":  {"width": 720,  "height": 1280, "bitrate": "5M",  "crf": 21},
    "1080p": {"width": 1080, "height": 1920, "bitrate": "10M", "crf": 19},
}

# Landscape (16:9) target resolutions and bitrates
LANDSCAPE_QUALITY: dict[str, dict] = {
    "480p":  {"width": 854,  "height": 480,  "bitrate": "2M",  "crf": 23},
    "720p":  {"width": 1280, "height": 720,  "bitrate": "5M",  "crf": 21},
    "1080p": {"width": 1920, "height": 1080, "bitrate": "10M", "crf": 19},
}

# Source download quality needed to get enough pixels after 9:16 crop.
SOURCE_QUALITY_FOR_VERTICAL: dict[str, str] = {
    "480p":  "1080p",
    "720p":  "1440p",
    "1080p": "2160p",
}


# ── FFmpeg helpers ───────────────────────────────────────────────────────

def _probe_video(path: str) -> dict:
    """Return width, height, duration, fps for the given video file via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-300:]}")

    data = json.loads(result.stdout)
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        raise RuntimeError(f"No video stream found in {path}")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))

    # Duration: prefer format-level for accuracy
    duration = float(data.get("format", {}).get("duration", 0)) or float(
        video_stream.get("duration", 0)
    )

    # Frame rate as fraction → float
    fps_str = video_stream.get("avg_frame_rate", "30/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) > 0 else 30.0
    except Exception:
        fps = 30.0

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "fps": fps,
    }


def _escape_ffmpeg_filter_path(path: str) -> str:
    """Escape a path for use inside an FFmpeg filter (e.g. ass=, subtitles=).

    The subtitles/ass filter in FFmpeg is parsed twice: first by the filter
    chain parser, then by libass. Special characters need escaping.
    """
    # Escape backslash first, then the colon, then single quote
    escaped = path.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    return escaped


def _run_ffmpeg(cmd: list[str], description: str = "ffmpeg") -> None:
    """Run an FFmpeg command, logging output and raising on failure."""
    logger.info(f"[{description}] Running: ffmpeg {' '.join(cmd[1:])[:300]}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"[{description}] FFmpeg failed:\nstderr: {result.stderr[-1000:]}")
        raise RuntimeError(f"FFmpeg {description} failed: {result.stderr[-500:]}")


# ── Public API ───────────────────────────────────────────────────────────

def generate_proxy_video(
    youtube_url: str,
    start_time: float,
    end_time: float,
    cached_video_path: str | None = None,
) -> str:
    """Generate a lightweight 16:9 proxy video for frontend preview editing.

    No cropping, no subtitles — just a trimmed and downscaled segment.
    Single-pass FFmpeg.
    """
    segment_path = None
    output_dir = tempfile.mkdtemp(prefix="clipper_proxy_")
    output_path = os.path.join(output_dir, "proxy.mp4")
    downloaded = False

    try:
        if cached_video_path:
            logger.info(f"Using cached video for proxy: {cached_video_path}")
            segment_path = cached_video_path
            need_trim = True
        else:
            segment_path = download_video_segment(
                youtube_url, start_time, end_time, quality="480p"
            )
            downloaded = True
            probe = _probe_video(segment_path)
            need_trim = probe["duration"] > (end_time - start_time) + 5

        clip_duration = end_time - start_time

        cmd = ["ffmpeg", "-y"]

        if need_trim:
            preroll = 0.1 if start_time >= 0.1 else 0.0
            cmd.extend(["-ss", f"{max(0.0, start_time - preroll):.3f}"])
            cmd.extend(["-i", segment_path])
            if preroll > 0:
                cmd.extend(["-ss", f"{preroll:.3f}"])
            cmd.extend(["-t", f"{clip_duration:.3f}"])
        else:
            cmd.extend(["-i", segment_path])
            cmd.extend(["-t", f"{clip_duration:.3f}"])

        cmd.extend([
            "-vf", "scale='min(854,iw)':-2:flags=fast_bilinear,format=yuv420p",
            "-c:v", _VCODEC,
            "-preset", _PRESET_FAST,
            "-crf", "30",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "96k",
            "-movflags", "+faststart",
            "-threads", _FFMPEG_THREADS,
            output_path,
        ])

        _run_ffmpeg(cmd, "proxy")
        logger.info(f"Generated proxy: {output_path}")
        return output_path

    finally:
        # Clean up only locally-downloaded segments (not cached files)
        if (
            downloaded
            and segment_path
            and os.path.exists(segment_path)
            and not segment_path.startswith("http")
        ):
            try:
                os.remove(segment_path)
                parent = os.path.dirname(segment_path)
                if parent and os.path.exists(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass


def generate_thumbnail(rendered_path: str) -> str:
    """Extract a frame at 1s as a JPEG thumbnail."""
    output_dir = tempfile.mkdtemp(prefix="clipper_thumb_")
    output_path = os.path.join(output_dir, "thumbnail.jpg")

    cmd = [
        "ffmpeg", "-y",
        "-ss", "1",
        "-i", rendered_path,
        "-frames:v", "1",
        "-q:v", "3",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not os.path.exists(output_path):
        # Fallback: try first frame
        cmd[2] = "-ss"
        cmd[3] = "0"
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError("Thumbnail generation failed")

    logger.info(f"Generated thumbnail: {output_path}")
    return output_path


def generate_preview_from_clip(rendered_path: str) -> str:
    """Create a lightweight preview MP4 (≤480px wide) for editor playback."""
    output_dir = tempfile.mkdtemp(prefix="clipper_preview_")
    output_path = os.path.join(output_dir, "preview.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-i", rendered_path,
        "-vf", "scale='min(480,iw)':-2:flags=fast_bilinear",
        "-c:v", _VCODEC,
        "-preset", _PRESET_FAST,
        "-crf", "32",
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        "-threads", _FFMPEG_THREADS,
        output_path,
    ]
    _run_ffmpeg(cmd, "preview")
    logger.info(f"Generated preview: {output_path}")
    return output_path


def render_clip(
    youtube_url: str,
    start_time: float,
    end_time: float,
    word_timestamps: list[dict],
    caption_style: str = "classic",
    quality: str = "480p",
    export_format: str = "9:16",
    crop_x_percent: float = 50.0,
    cached_video_path: str | None = None,
) -> tuple[str, str]:
    """Render a clip with cropping, resizing and burned-in captions in a
    single FFmpeg pass.

    Pipeline (one ffmpeg invocation):
        1. Trim [start_time, end_time]
        2. Crop to 9:16 at crop_x_percent (or keep 16:9)
        3. Scale to target resolution
        4. Burn ASS subtitles (if caption_style != "none")
        5. Encode H.264 + AAC, faststart

    Args:
        youtube_url: YouTube URL (used if cached_video_path is None).
        start_time: Clip start in seconds.
        end_time: Clip end in seconds.
        word_timestamps: Word-level timestamps (clip-relative or absolute).
        caption_style: One of the ASS preset names, "classic", "hormozi",
            "hormozi_dynamic", "impact_pop", "typewriter", "kenergy",
            "midnight", "cleanboss", "staccato", "serenity", or "none".
        quality: Output quality ("480p", "720p", "1080p").
        export_format: "9:16" or "16:9".
        crop_x_percent: Horizontal crop position (0=left, 50=center, 100=right).
        cached_video_path: Optional local path to a cached source video.

    Returns:
        (output_path, effective_quality) — effective_quality may differ from
        the requested quality if the source lacked enough pixels for a
        clean 9:16 crop.
    """
    segment_path = None
    output_dir = tempfile.mkdtemp(prefix="clipper_render_")
    output_path = os.path.join(output_dir, "output.mp4")
    ass_path: str | None = None
    downloaded_segment = False

    try:
        # ── Step 1: Resolve source video ──────────────────────────────────
        if cached_video_path:
            logger.info(f"Using cached source: {cached_video_path}")
            segment_path = cached_video_path
        else:
            if export_format == "9:16":
                source_quality = SOURCE_QUALITY_FOR_VERTICAL.get(quality, "best")
            else:
                source_quality = quality

            logger.info(f"Downloading from YouTube (source={source_quality})")
            segment_path = download_video_segment(
                youtube_url, start_time, end_time, quality=source_quality
            )
            downloaded_segment = True

        # ── Step 2: Probe source ──────────────────────────────────────────
        probe = _probe_video(segment_path)
        src_w = probe["width"]
        src_h = probe["height"]
        src_duration = probe["duration"]

        # Determine if we still need to trim (cached or full-video downloads)
        clip_duration = end_time - start_time
        needs_trim = src_duration > clip_duration + 5

        logger.info(
            f"Source: {src_w}x{src_h} duration={src_duration:.1f}s | "
            f"Clip: [{start_time:.1f}-{end_time:.1f}]s ({clip_duration:.1f}s) | "
            f"Trim={needs_trim}"
        )

        # ── Step 3: Compute crop and target resolution ────────────────────
        if export_format == "9:16":
            target_ratio = 9 / 16
            current_ratio = src_w / src_h

            if current_ratio > target_ratio:
                # Source is wider than 9:16 → crop horizontally
                crop_w = int(src_h * target_ratio)
                # Make crop_w even (h264 requires even dims)
                crop_w = crop_w - (crop_w % 2)
                max_offset = src_w - crop_w
                crop_x = int((crop_x_percent / 100.0) * max_offset)
                crop_x = max(0, min(crop_x, max_offset))
                # Make crop_x even for clean codec alignment
                crop_x = crop_x - (crop_x % 2)
                crop_h = src_h
                crop_y = 0
            else:
                # Source is taller than 9:16 → crop vertically (center)
                crop_h = int(src_w / target_ratio)
                crop_h = crop_h - (crop_h % 2)
                crop_w = src_w
                crop_x = 0
                crop_y = max(0, (src_h - crop_h) // 2)
                crop_y = crop_y - (crop_y % 2)

            # Auto-cap quality if crop width is smaller than target
            ACHIEVABLE = {"1080p": 1080, "720p": 720, "480p": 480}
            if crop_w < ACHIEVABLE.get(quality, 0):
                for tier in ("1080p", "720p", "480p"):
                    if crop_w >= ACHIEVABLE[tier]:
                        logger.warning(
                            f"Quality auto-capped {quality} → {tier}: "
                            f"crop width {crop_w}px is too small to produce "
                            f"{quality} without upscaling "
                            f"(need ≥{ACHIEVABLE.get(quality, 0)}px)"
                        )
                        quality = tier
                        break

            vq = VERTICAL_QUALITY.get(quality, VERTICAL_QUALITY["480p"])
        else:
            # 16:9 — no horizontal cropping, just scale
            crop_w = src_w
            crop_h = src_h
            crop_x = 0
            crop_y = 0
            vq = LANDSCAPE_QUALITY.get(quality, LANDSCAPE_QUALITY["480p"])

        target_w = vq["width"]
        target_h = vq["height"]
        target_bitrate = vq["bitrate"]
        target_crf = vq["crf"]

        # ── Step 4: Generate ASS subtitles (if needed) ────────────────────
        # Map all caption styles through the ASS pipeline. "classic" and
        # "hormozi" are now styled via ASS presets (not Pillow).
        ASS_STYLES = {
            "classic", "hormozi", "hormozi_dynamic",
            "impact_pop", "typewriter",
            "kenergy", "midnight", "cleanboss", "staccato", "serenity",
        }
        burn_subtitles = (
            caption_style in ASS_STYLES
            and len(word_timestamps) > 0
        )

        if burn_subtitles:
            # Shift word timestamps to clip-relative time
            shifted_words = [
                {
                    "word": w["word"],
                    "start": max(0, w["start"] - start_time),
                    "end": max(0, w["end"] - start_time),
                }
                for w in word_timestamps
            ]

            ass_path = os.path.join(output_dir, "subtitles.ass")
            generate_dynamic_ass(
                word_level_data=shifted_words,
                output_ass_path=ass_path,
                style_name=caption_style,
                video_width=target_w,
                video_height=target_h,
                max_words_per_line=4,
            )

        # ── Step 5: Build single-pass FFmpeg filter graph ─────────────────
        # Order matters: trim → crop → scale → subtitles
        # (Trim with -ss/-to as input options for accurate seeking + speed.)
        filters: list[str] = []

        # Crop only if needed
        if export_format == "9:16" and (crop_w != src_w or crop_h != src_h):
            filters.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")

        # Scale only if dimensions differ
        if crop_w != target_w or crop_h != target_h:
            filters.append(
                f"scale={target_w}:{target_h}:flags=lanczos"
            )

        # Burn subtitles
        if burn_subtitles and ass_path:
            escaped = _escape_ffmpeg_filter_path(ass_path)
            filters.append(f"ass='{escaped}'")

        # Force output pixel format for max compatibility
        filters.append("format=yuv420p")

        vf = ",".join(filters) if filters else "null"

        # ── Step 6: Encode in one pass ────────────────────────────────────
        cmd = ["ffmpeg", "-y"]

        # Trim strategy:
        #  - If source contains the full video (cached/full download), use the
        #    "two-stage seek" pattern: fast keyframe seek BEFORE -i, accurate
        #    output trim AFTER -i. This is the recommended fast+accurate idiom.
        #  - If source is already pre-trimmed (download_video_segment), no trim.
        if needs_trim:
            # Pre-roll: seek to a keyframe just before start_time, then
            # use output -ss to refine to the exact frame.
            preroll = 0.1 if start_time >= 0.1 else 0.0
            cmd.extend(["-ss", f"{max(0.0, start_time - preroll):.3f}"])
            cmd.extend(["-i", segment_path])
            if preroll > 0:
                cmd.extend(["-ss", f"{preroll:.3f}"])
            cmd.extend(["-t", f"{clip_duration:.3f}"])
        else:
            cmd.extend(["-i", segment_path])
            # Cap output to clip_duration as a safety guard
            cmd.extend(["-t", f"{clip_duration:.3f}"])

        cmd.extend([
            "-vf", vf,
            "-c:v", _VCODEC,
            "-preset", _PRESET_BALANCED,
            "-crf", str(target_crf),
            "-maxrate", target_bitrate,
            "-bufsize", f"{int(target_bitrate.rstrip('M')) * 2}M",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-movflags", "+faststart",
            "-threads", _FFMPEG_THREADS,
            output_path,
        ])

        logger.info(
            f"Rendering clip [{caption_style}] @ {quality} "
            f"({target_w}x{target_h}) — single-pass FFmpeg"
        )
        _run_ffmpeg(cmd, f"render-{caption_style}")

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(f"Output file is empty or missing: {output_path}")

        out_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(
            f"Render complete: {output_path} ({out_size_mb:.2f}MB) "
            f"effective_quality={quality}"
        )

        return output_path, quality

    finally:
        # Cleanup ASS subtitle file
        if ass_path and os.path.exists(ass_path):
            try:
                os.remove(ass_path)
            except OSError:
                pass

        # Cleanup downloaded segment (only if WE downloaded it)
        if downloaded_segment and segment_path and os.path.exists(segment_path):
            if isinstance(segment_path, str) and not segment_path.startswith("http"):
                try:
                    os.remove(segment_path)
                    parent = os.path.dirname(segment_path)
                    if parent and os.path.exists(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                except OSError:
                    pass
