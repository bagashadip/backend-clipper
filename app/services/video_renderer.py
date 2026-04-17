import os
import logging
import tempfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Pillow 10+ removed ANTIALIAS; moviepy's resize still references it
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import VideoFileClip, VideoClip, CompositeVideoClip
from app.services.youtube import download_video_segment

logger = logging.getLogger(__name__)

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

# Caption style presets
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
        segment_path = download_video_segment(youtube_url, start_time, end_time, quality="480p")

        video = VideoFileClip(segment_path)

        # Force 480p resolution (854x480) for lightweight proxy
        cur_w, cur_h = video.size
        if cur_w > 854 or cur_h > 480:
            logger.info(f"Proxy: resizing from {cur_w}x{cur_h} to 854x480")
            video = video.resize((854, 480))

        output_dir = tempfile.mkdtemp(prefix="clipper_proxy_")
        output_path = os.path.join(output_dir, "proxy.mp4")

        video.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            fps=24,
            preset="ultrafast",
            threads=2,
            logger=None,
            temp_audiofile=os.path.join(output_dir, "temp-audio.m4a"),
            remove_temp=True,
            ffmpeg_params=["-crf", "32", "-movflags", "+faststart"],
        )

        video.close()
        return output_path

    finally:
        if segment_path and os.path.exists(segment_path):
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
        "-c:v", "libx264", "-crf", "32", "-preset", "ultrafast",
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
) -> str:
    """
    Render a clip with burned-in captions.

    Args:
        youtube_url: Source YouTube URL.
        start_time: Clip start in seconds.
        end_time: Clip end in seconds.
        word_timestamps: List of {"word": str, "start": float, "end": float}.
        caption_style: "classic", "hormozi", or "none".
        quality: "480p", "720p", or "1080p".
        export_format: "9:16" (portrait) or "16:9" (landscape).
        crop_x_percent: 0-100 crop horizontal position (portrait only).

    Returns:
        Path to the rendered MP4 file.
    """
    segment_path = None
    try:
        # Download source — portrait needs higher res for crop
        if export_format == "9:16":
            source_quality = SOURCE_QUALITY_FOR_VERTICAL.get(quality, quality)
        else:
            source_quality = quality  # landscape doesn't need oversampling

        logger.info(f"Downloading segment {start_time:.1f}s - {end_time:.1f}s "
                    f"(source={source_quality}, format={export_format}, crop={crop_x_percent:.0f}%)")
        segment_path = download_video_segment(youtube_url, start_time, end_time, quality=source_quality)

        video = VideoFileClip(segment_path)

        # Crop & resize based on export format
        if export_format == "9:16":
            video = _crop_to_vertical(video, crop_x_percent=crop_x_percent)
            vq = VERTICAL_QUALITY.get(quality, VERTICAL_QUALITY["480p"])
        else:
            # 16:9 — no crop needed, just resize
            vq = LANDSCAPE_QUALITY.get(quality, LANDSCAPE_QUALITY["480p"])

        target_w, target_h = vq["width"], vq["height"]
        cur_w, cur_h = video.size
        if cur_w != target_w or cur_h != target_h:
            logger.info(f"Resizing from {cur_w}x{cur_h} to {target_w}x{target_h}")
            video = video.resize((target_w, target_h))

        # Apply face tracking placeholder
        video = _apply_face_tracking(video)

        # Burn in captions
        style_config = CAPTION_STYLES.get(caption_style)
        if style_config:
            video = _burn_captions(video, word_timestamps, start_time, style_config)

        # Export
        output_dir = tempfile.mkdtemp(prefix="clipper_render_")
        output_path = os.path.join(output_dir, "output.mp4")

        logger.info(f"Rendering clip to {output_path} at {quality}")
        video.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            audio_bitrate="192k",
            bitrate=vq["bitrate"],
            fps=30,
            preset="medium",
            threads=2,
            logger=None,
            temp_audiofile=os.path.join(output_dir, "temp-audio.m4a"),
            remove_temp=True,
            ffmpeg_params=["-movflags", "+faststart", "-crf", "18"],
        )

        video.close()
        return output_path

    finally:
        # Cleanup downloaded segment
        if segment_path and os.path.exists(segment_path):
            try:
                os.remove(segment_path)
                # Remove parent temp dir
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
