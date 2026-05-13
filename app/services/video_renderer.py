import os
import logging
import subprocess
import tempfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Pillow 10+ removed ANTIALIAS; moviepy's resize still references it
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import VideoFileClip, VideoClip, CompositeVideoClip
from app.services.youtube import download_video_segment
from app.services.subtitle_ass import generate_dynamic_ass, burn_ass_subtitles

logger = logging.getLogger(__name__)

logger.info("video_renderer: Using CPU encoding (libx264)")
_VCODEC = "libx264"
_PRESET_FAST = "ultrafast"
_PRESET_BALANCED = "medium"


def _cq_params(cq: int, preset: str) -> list[str]:
    """Return ffmpeg quality params for the active encoder.

    x264 uses ``-crf CQ``.
    """
    return ["-crf", str(cq)]

# Find a bold font that exists on the system
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

def _find_font():
    for p in _FONT_PATHS:
        if os.path.exists(p):
            return p
    return None

_BOLD_FONT_PATH = _find_font()

# Caption style presets (used by OLD Pillow-based pipeline for "classic" and "hormozi")
CAPTION_STYLES = {
    "classic": {
        "fontsize": 60,
        "color": (255, 255, 255),
        "stroke_color": (0, 0, 0),
        "stroke_width": 3,
    },
    "hormozi": {
        "fontsize": 70,
        "color": (255, 255, 0),
        "stroke_color": (0, 0, 0),
        "stroke_width": 4,
    },
    "none": None,
    # "hormozi_dynamic" is handled by the ASS pipeline — no Pillow config needed
}

# Vertical (9:16) target resolutions and bitrates
VERTICAL_QUALITY = {
    "480p":  {"width": 480,  "height": 854,  "bitrate": "2M"},
    "720p":  {"width": 720,  "height": 1280, "bitrate": "5M"},
    "1080p": {"width": 1080, "height": 1920, "bitrate": "10M"},
}

# Landscape (16:9) target resolutions and bitrates
LANDSCAPE_QUALITY = {
    "480p":  {"width": 854,  "height": 480,  "bitrate": "2M"},
    "720p":  {"width": 1280, "height": 720,  "bitrate": "5M"},
    "1080p": {"width": 1920, "height": 1080, "bitrate": "10M"},
}

# Source download quality needed to get enough pixels after 9:16 crop.
# After cropping 16:9→9:16, crop_width = source_height * 9/16.
# We need crop_width >= target vertical width, so download higher res.
#   480p vertical (480w) → 1080p source (crop=607) → downscale = sharp
#   720p vertical (720w) → 1440p source (crop=810) → downscale = sharp
#  1080p vertical (1080w) → 2160p source (crop=1215) → downscale = sharp
SOURCE_QUALITY_FOR_VERTICAL = {
    "480p":  "1080p",
    "720p":  "1440p",
    "1080p": "2160p",
}


def generate_proxy_video(
    youtube_url: str,
    start_time: float,
    end_time: float,
    cached_video_path: str | None = None,
) -> str:
    """
    Generate a lightweight 16:9 proxy video for frontend preview editing.
    No cropping, no subtitles — just a trimmed segment.

    Returns:
        Path to the proxy MP4 file.
    """
    segment_path = None
    try:
        logger.info(f"Generating proxy for {start_time:.1f}s - {end_time:.1f}s")
        if cached_video_path:
            logger.info(f"Using cached video source for proxy: {cached_video_path}")
            segment_path = cached_video_path
        else:
            segment_path = download_video_segment(youtube_url, start_time, end_time, quality="480p")

        video = VideoFileClip(segment_path)
        video_duration = video.duration

        # If full video was downloaded/cached, trim to clip window
        if video_duration > (end_time - start_time) + 5:
            logger.info(f"Trimming proxy to [{start_time:.1f}s, {end_time:.1f}s]")
            video = video.subclip(start_time, min(end_time, video_duration))

        # Force 480p resolution (854x480) for lightweight proxy
        cur_w, cur_h = video.size
        if cur_w > 854 or cur_h > 480:
            logger.info(f"Proxy: resizing from {cur_w}x{cur_h} to 854x480")
            video = video.resize((854, 480))

        output_dir = tempfile.mkdtemp(prefix="clipper_proxy_")
        output_path = os.path.join(output_dir, "proxy.mp4")

        video.write_videofile(
            output_path,
            codec=_VCODEC,
            audio_codec="aac",
            fps=24,
            preset=_PRESET_FAST,
            threads=2,
            logger=None,
            temp_audiofile=os.path.join(output_dir, "temp-audio.m4a"),
            remove_temp=True,
            ffmpeg_params=_cq_params(32, _PRESET_FAST) + ["-movflags", "+faststart"],
        )

        video.close()
        return output_path

    finally:
        # Only cleanup if it's a local file (not an S3 URL) and we downloaded it
        if not cached_video_path and segment_path and os.path.exists(segment_path):
            if isinstance(segment_path, str) and not segment_path.startswith("http"):
                try:
                    os.remove(segment_path)
                    os.rmdir(os.path.dirname(segment_path))
                except OSError:
                    pass


def generate_thumbnail(rendered_path: str) -> str:
    """
    Extract a frame at 1 second (or first frame) from the rendered clip as a JPEG thumbnail.
    Returns path to the thumbnail file.
    """
    import subprocess

    output_dir = tempfile.mkdtemp(prefix="clipper_thumb_")
    output_path = os.path.join(output_dir, "thumbnail.jpg")

    cmd = [
        "ffmpeg", "-i", rendered_path,
        "-ss", "1", "-frames:v", "1",
        "-q:v", "3",
        "-y", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not os.path.exists(output_path):
        # Fallback: try first frame
        cmd[cmd.index("-ss") + 1] = "0"
        subprocess.run(cmd, check=True, capture_output=True)

    logger.info(f"Generated thumbnail: {output_path}")
    return output_path


def generate_preview_from_clip(rendered_path: str) -> str:
    """
    Create a lightweight preview version from a rendered clip for editor playback.
    Uses ffmpeg directly for speed — ultrafast preset, CRF 32, max 480px wide.

    Returns:
        Path to the preview MP4 file.
    """
    import subprocess

    output_dir = tempfile.mkdtemp(prefix="clipper_preview_")
    output_path = os.path.join(output_dir, "preview.mp4")

    cmd = [
        "ffmpeg", "-i", rendered_path,
        "-vf", "scale='min(480,iw)':-2",
        "-c:v", _VCODEC, "-preset", _PRESET_FAST,
        *_cq_params(32, _PRESET_FAST),
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        "-y", output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
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
    """
    Render a clip with burned-in captions.
    
    CAPTION STYLE ROUTING:
      - "classic" / "hormozi": Legacy Pillow frame-by-frame text overlay
      - "hormozi_dynamic": ASS subtitle pipeline (per-word karaoke highlighting)
      - "none": No captions, single-pass export
    
    Args:
        youtube_url: YouTube URL (used if cached_video_path is None)
        start_time: Start time in seconds
        end_time: End time in seconds
        word_timestamps: List of word-level timestamps for captions
        caption_style: Caption style to use
        quality: Target output quality
        export_format: Output format (9:16 or 16:9)
        crop_x_percent: Horizontal crop position for 9:16
        cached_video_path: Optional S3 URL or local path to cached source video

    Returns:
        (output_path, effective_quality) -- effective_quality may differ from
        `quality` if the source lacked enough pixels to avoid upscaling.
    """
    segment_path = None
    try:
        # ── Step 1: Get source video (from cache or download) ────────────
        if cached_video_path:
            # Use cached video (local path from S3 download)
            logger.info(f"Using cached video source: {cached_video_path}")
            segment_path = cached_video_path
            use_network_stream = False
        else:
            # Download from YouTube
            if export_format == "9:16":
                source_quality = SOURCE_QUALITY_FOR_VERTICAL.get(quality, "best")
            else:
                source_quality = quality
            
            logger.info(f"Downloading from YouTube (source={source_quality})")
            segment_path = download_video_segment(youtube_url, start_time, end_time, quality=source_quality)
            use_network_stream = False
        
        # ── Step 2: Load video and trim if needed ─────────────────────────
        video = VideoFileClip(segment_path)
        video_duration = video.duration
        
        # If full video was downloaded/cached, trim to clip window
        if video_duration > (end_time - start_time) + 5:
            logger.info(f"Trimming to [{start_time:.1f}s, {end_time:.1f}s]")
            video = video.subclip(start_time, min(end_time, video_duration))

        # ── Step 2: Crop & resize ─────────────────────────────────────────
        if export_format == "9:16":
            video = _crop_to_vertical(video, crop_x_percent=crop_x_percent)
            crop_w, crop_h = video.size

            # Auto-cap quality: 9:16 crop from a 16:9 source gives crop_w = src_h * 9/16.
            # If crop_w < requested output width we'd upscale, producing the same soft look
            # as a lower-quality downscale.  Use the highest tier we can produce cleanly.
            ACHIEVABLE: dict[str, int] = {"1080p": 1080, "720p": 720, "480p": 480}
            if crop_w < ACHIEVABLE.get(quality, 0):
                for tier in ("1080p", "720p", "480p"):
                    if crop_w >= ACHIEVABLE[tier]:
                        logger.warning(
                            f"Quality auto-capped {quality} → {tier}: "
                            f"crop width {crop_w}px is too small to produce "
                            f"{quality} without upscaling "
                            f"(need ≥{ACHIEVABLE.get(quality,0)}px)"
                        )
                        quality = tier
                        break

            vq = VERTICAL_QUALITY.get(quality, VERTICAL_QUALITY["480p"])
        else:
            vq = LANDSCAPE_QUALITY.get(quality, LANDSCAPE_QUALITY["480p"])

        target_w, target_h = vq["width"], vq["height"]
        cur_w, cur_h = video.size
        if cur_w != target_w or cur_h != target_h:
            logger.info(f"Resizing from {cur_w}x{cur_h} to {target_w}x{target_h}")
            video = video.resize((target_w, target_h))

        video = _apply_face_tracking(video)

        output_dir = tempfile.mkdtemp(prefix="clipper_render_")
        output_path = os.path.join(output_dir, "output.mp4")

        # ── Step 3: CAPTION STYLE ROUTING ─────────────────────────────────

        if caption_style in ("hormozi_dynamic", "impact_pop", "typewriter", "kenergy", "midnight", "cleanboss", "staccato", "serenity") and len(word_timestamps) > 0:
            # ────────────────────────────────────────────────────────────
            # ROUTE A: New ASS pipeline (per-word karaoke highlighting)
            # ────────────────────────────────────────────────────────────
            intermediate_path = os.path.join(output_dir, "intermediate.mp4")

            # Pass 1: Export cropped/resized video without captions
            logger.info(f"[{caption_style}] Pass 1: Exporting intermediate → {intermediate_path}")
            video.write_videofile(
                intermediate_path,
                codec=_VCODEC,
                audio_codec="aac",
                audio_bitrate="192k",
                fps=30,
                preset=_PRESET_FAST,
                threads=2,
                logger=None,
                temp_audiofile=os.path.join(output_dir, "temp-audio.m4a"),
                remove_temp=True,
                ffmpeg_params=_cq_params(16, _PRESET_FAST) + ["-movflags", "+faststart"],
            )
            video.close()

            # Shift word timestamps to clip-relative time
            shifted_words = []
            for w in word_timestamps:
                shifted_words.append({
                    "word": w["word"],
                    "start": max(0, w["start"] - start_time),
                    "end": max(0, w["end"] - start_time),
                })

            # Pass 2: Generate ASS and burn via FFmpeg
            ass_path = os.path.join(output_dir, "subtitles.ass")
            generate_dynamic_ass(
                word_level_data=shifted_words,
                output_ass_path=ass_path,
                style_name=caption_style,
                video_width=target_w,
                video_height=target_h,
                max_words_per_line=4,
            )

            logger.info(f"[{caption_style}] Pass 2: Burning ASS subtitles → {output_path}")
            burn_ass_subtitles(
                input_video_path=intermediate_path,
                ass_path=ass_path,
                output_video_path=output_path,
                cq=18,
                preset=_PRESET_BALANCED,
                bitrate=vq["bitrate"],
            )

            # Cleanup intermediate
            try:
                os.remove(intermediate_path)
            except OSError:
                pass

        elif caption_style in ("classic", "hormozi") and len(word_timestamps) > 0:
            # ────────────────────────────────────────────────────────────
            # ROUTE B: Legacy Pillow frame-by-frame text overlay
            # ────────────────────────────────────────────────────────────
            style_config = CAPTION_STYLES.get(caption_style)
            if style_config:
                logger.info(f"[{caption_style}] Burning captions via Pillow pipeline")
                video = _burn_captions(video, word_timestamps, start_time, style_config)

            logger.info(f"Rendering clip to {output_path} at {quality}")
            video.write_videofile(
                output_path,
                codec=_VCODEC,
                audio_codec="aac",
                audio_bitrate="192k",
                bitrate=vq["bitrate"],
                fps=30,
                preset=_PRESET_BALANCED,
                threads=2,
                logger=None,
                temp_audiofile=os.path.join(output_dir, "temp-audio.m4a"),
                remove_temp=True,
                ffmpeg_params=["-movflags", "+faststart"] + _cq_params(18, _PRESET_BALANCED),
            )
            video.close()

        else:
            # ────────────────────────────────────────────────────────────
            # ROUTE C: No captions ("none" or empty word_timestamps)
            # ────────────────────────────────────────────────────────────
            logger.info(f"Rendering clip (no captions) to {output_path} at {quality}")
            video.write_videofile(
                output_path,
                codec=_VCODEC,
                audio_codec="aac",
                audio_bitrate="192k",
                bitrate=vq["bitrate"],
                fps=30,
                preset=_PRESET_BALANCED,
                threads=2,
                logger=None,
                temp_audiofile=os.path.join(output_dir, "temp-audio.m4a"),
                remove_temp=True,
                ffmpeg_params=["-movflags", "+faststart"] + _cq_params(18, _PRESET_BALANCED),
            )
            video.close()

        return output_path, quality

    finally:
        # Only cleanup if it's a local file (not an S3 URL)
        if segment_path and os.path.exists(segment_path) if isinstance(segment_path, str) and not segment_path.startswith("http") else False:
            try:
                os.remove(segment_path)
                os.rmdir(os.path.dirname(segment_path))
            except OSError:
                pass


def _crop_to_vertical(video: VideoFileClip, crop_x_percent: float = 50.0) -> VideoFileClip:
    """Crop a 16:9 video to 9:16 using crop_x_percent for horizontal position.
    
    crop_x_percent: 0 = flush left, 50 = center, 100 = flush right.
    """
    w, h = video.size

    # Target aspect ratio 9:16
    target_ratio = 9 / 16
    current_ratio = w / h

    if current_ratio > target_ratio:
        # Video is wider than 9:16, crop width using crop_x_percent
        new_w = int(h * target_ratio)
        max_offset = w - new_w
        x1 = int((crop_x_percent / 100.0) * max_offset)
        x1 = max(0, min(x1, max_offset))
        video = video.crop(x1=x1, y1=0, x2=x1 + new_w, y2=h)
    else:
        # Video is taller, crop height (center)
        new_h = int(w / target_ratio)
        y_center = h // 2
        y1 = y_center - new_h // 2
        video = video.crop(x1=0, y1=y1, x2=w, y2=y1 + new_h)

    return video


def _apply_face_tracking(video: VideoFileClip) -> VideoFileClip:
    """
    PLACEHOLDER: Face tracking logic.

    In production, this would:
    1. Detect faces in each frame using a model (e.g., MediaPipe, MTCNN).
    2. Track the primary speaker's face across frames.
    3. Dynamically adjust the crop region to keep the face centered.

    For now, returns the video unchanged (using center crop).
    """
    # TODO: Implement face tracking with MediaPipe or similar
    # - Detect face bounding box per frame
    # - Smooth the tracking across frames to avoid jitter
    # - Adjust crop region dynamically
    return video


def _burn_captions(
    video: VideoFileClip,
    word_timestamps: list[dict],
    clip_start: float,
    style: dict,
) -> VideoFileClip:
    """Burn dynamic captions (2-3 words at a time) using Pillow (no ImageMagick)."""
    w, h = video.size

    if not _BOLD_FONT_PATH:
        logger.warning("No bold font found on system, skipping captions")
        return video

    # Scale font size relative to video width (style fontsize is tuned for 1080px width)
    base_fontsize = style["fontsize"]
    fontsize = max(16, int(base_fontsize * (w / 1080)))
    font = ImageFont.truetype(_BOLD_FONT_PATH, fontsize)
    text_color = style["color"]
    stroke_color = style["stroke_color"]
    stroke_width = max(1, int(style["stroke_width"] * (w / 1080)))

    # Group words into chunks of 3
    word_groups = _group_words(word_timestamps, group_size=3)

    # Build a timeline: for each frame time, which caption text to show
    captions_timeline = []
    for group in word_groups:
        text = " ".join(wd["word"] for wd in group).upper()
        group_start = group[0]["start"] - clip_start
        group_end = group[-1]["end"] - clip_start
        if group_start < 0:
            group_start = 0
        if group_end <= group_start:
            continue
        captions_timeline.append((group_start, group_end, text))

    if not captions_timeline:
        return video

    logger.info(f"Burning {len(captions_timeline)} caption groups onto video")

    def _make_caption_frame(get_frame, t):
        """Overlay caption text on each frame using Pillow."""
        frame = get_frame(t)

        # Find which caption is active at time t
        active_text = None
        for start, end, text in captions_timeline:
            if start <= t < end:
                active_text = text
                break

        if active_text is None:
            return frame

        # Convert frame to PIL Image
        img = Image.fromarray(frame)
        draw = ImageDraw.Draw(img)

        # Calculate text position (center, 65% down)
        max_width = int(w * 0.85)
        bbox = draw.textbbox((0, 0), active_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Word wrap if needed
        if text_w > max_width:
            # Simple word wrap
            words = active_text.split()
            lines = []
            current_line = ""
            for word in words:
                test = f"{current_line} {word}".strip()
                tw = draw.textbbox((0, 0), test, font=font)[2]
                if tw > max_width and current_line:
                    lines.append(current_line)
                    current_line = word
                else:
                    current_line = test
            if current_line:
                lines.append(current_line)
            active_text = "\n".join(lines)
            bbox = draw.textbbox((0, 0), active_text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

        x = (w - text_w) // 2
        y = int(h * 0.65)

        # Draw stroke (outline)
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), active_text, font=font, fill=stroke_color)

        # Draw main text
        draw.text((x, y), active_text, font=font, fill=text_color)

        return np.array(img)

    # Create a new video clip with captions burned in
    captioned = video.fl(_make_caption_frame)
    captioned = captioned.set_audio(video.audio)
    return captioned


def _group_words(words: list[dict], group_size: int = 3) -> list[list[dict]]:
    """
    Group words into chunks for caption display.

    Groups are split when:
    - group_size words are reached, OR
    - there's a timing gap > 0.3s between consecutive words
    """
    if not words:
        return []

    GAP_THRESHOLD = 0.3  # seconds — gap between words that forces a new group

    groups: list[list[dict]] = []
    current_group: list[dict] = [words[0]]

    for i in range(1, len(words)):
        prev_end = words[i - 1].get("end", 0)
        curr_start = words[i].get("start", 0)
        gap = curr_start - prev_end

        if len(current_group) >= group_size or gap > GAP_THRESHOLD:
            groups.append(current_group)
            current_group = [words[i]]
        else:
            current_group.append(words[i])

    if current_group:
        groups.append(current_group)

    return groups
