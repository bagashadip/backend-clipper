"""
ASS (Advanced SubStation Alpha) subtitle generator with Hormozi-style
word-level highlighting.

For each word group (chunk), individual dialogue events are created where
the ACTIVE word is highlighted in a different color (yellow) while the rest
of the sentence stays white.  This creates the popular "karaoke" effect
seen in Alex Hormozi's short-form content.
"""

import os
import subprocess
import logging

logger = logging.getLogger(__name__)


def _probe_nvenc() -> bool:
    """Return False, forcing CPU mode."""
    return False

_GPU_AVAILABLE: bool = False
logger.info("subtitle_ass: using CPU encoding (libx264)")

# ── ASS color helpers ─────────────────────────────────────────────────────
# ASS uses BGR hex in the format &HBBGGRR& (alpha prefix &HAABBGGRR& optional)

_COLOR_WHITE = "&H00FFFFFF&"
_COLOR_YELLOW = "&H0000FFFF&"   # BGR for yellow (R=FF, G=FF, B=00)
_COLOR_BLACK = "&H00000000&"
_COLOR_GREEN = "&H0000FF00&"   # BGR for green (R=00, G=FF, B=00)
_COLOR_ORANGE = "&H0000A5FF&"  # BGR for orange (R=FF, G=A5, B=00)
_COLOR_CYAN = "&H00FFFF00&"    # BGR for cyan  (R=00, G=FF, B=FF)

# ── Additional colors for new styles ──────────────────────────────────────
_COLOR_ORANGE_RED = "&H000045FF&"   # #FF4500 → orange-red (kenergy)
_COLOR_NEON_GREEN = "&H0088FF00&"   # #00FF88 → neon green (midnight)
_COLOR_DIM_GRAY = "&H00AAAAAA&"     # #AAAAAA → dim gray (midnight/cleanboss)
_COLOR_LIGHT_GRAY = "&H00CCCCCC&"   # #CCCCCC → light gray (cleanboss)
_COLOR_HOT_PINK = "&H00B469FF&"     # #FF69B4 → hot pink (serenity)
_COLOR_LAVENDER = "&H00E0D5E8&"     # #E8D5E0 → muted lavender (serenity)
_COLOR_SOFT_BLACK = "&H00333333&"   # #333333 → soft black outline (cleanboss)
_COLOR_TRANS_BLACK = "&H40000000&"  # semi-transparent black (cleanboss shadow)

# ── Style presets ─────────────────────────────────────────────────────────

STYLE_PRESETS = {
    "hormozi_dynamic": {
        "fontname": "DejaVu Sans",    # Use "Impact" or "Arial Black" on Windows
        "fontsize": 18,           # Scaled by PlayResY — tuned for 384
        "primary_color": _COLOR_WHITE,
        "highlight_color": _COLOR_YELLOW,
        "outline_color": _COLOR_BLACK,
        "back_color": "&H80000000&",
        "bold": -1,               # -1 = true bold
        "outline": 3,
        "shadow": 1,
        "alignment": 2,           # bottom-center
        "margin_v": 60,
    },
    "classic": {
        "fontname": "DejaVu Sans",    # Use "Impact" or "Arial Black" on Windows
        "fontsize": 16,
        "primary_color": _COLOR_WHITE,
        "highlight_color": _COLOR_WHITE,   # No highlight for classic
        "outline_color": _COLOR_BLACK,
        "back_color": "&H00000000&",
        "bold": -1,
        "outline": 2,
        "shadow": 0,
        "alignment": 2,
        "margin_v": 60,
    },
    "impact_pop": {
        "fontname": "DejaVu Sans",
        "fontsize": 20,
        "primary_color": _COLOR_WHITE,
        "highlight_color": _COLOR_CYAN,
        "outline_color": _COLOR_BLACK,
        "back_color": "&H00000000&",
        "bold": -1,
        "outline": 3,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 55,
        # Per-word highlight overrides
        "highlight_outline_color": _COLOR_BLACK,
        "highlight_back_color": _COLOR_CYAN,
        "highlight_outline": 3,
        "highlight_shadow": 2,
        # Animation: flash-pop-settle
        "animate_active": True,
        "animate_flash_color": _COLOR_WHITE,
        "animate_glow_blur": 2.5,
        "animate_pop_scale": 108,
        "animate_pop_duration": 100,
        "animate_settle_duration": 80,
    },
    "typewriter": {
        "fontname": "Courier New",
        "fontsize": 18,
        "primary_color": _COLOR_WHITE,
        "highlight_color": _COLOR_WHITE,
        "outline_color": _COLOR_BLACK,
        "back_color": "&H00000000&",
        "bold": 0,
        "outline": 1,
        "shadow": 0,
        "alignment": 2,   # bottom-center
        "margin_v": 60,
    },
    # ── NEW: Viral 2025-2026 styles ───────────────────────────────────────
    "kenergy": {
        "fontname": "DejaVu Sans",
        "fontsize": 19,
        "primary_color": _COLOR_WHITE,
        "highlight_color": _COLOR_ORANGE_RED,
        "outline_color": _COLOR_BLACK,
        "back_color": "&H00000000&",
        "bold": -1,
        "outline": 3,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 55,
        # Flash-white → snap to orange-red (fast, aggressive)
        "animate_active": True,
        "animate_flash_color": _COLOR_WHITE,
        "animate_glow_blur": 0,
        "animate_pop_scale": 112,
        "animate_pop_duration": 50,
        "animate_settle_duration": 60,
    },
    "midnight": {
        "fontname": "DejaVu Sans",
        "fontsize": 18,
        "primary_color": _COLOR_DIM_GRAY,
        "highlight_color": _COLOR_NEON_GREEN,
        "outline_color": "&H00000000&",  # no outline — glow replaces it
        "back_color": "&H00000000&",
        "bold": -1,
        "outline": 0,
        "shadow": 0,
        "alignment": 2,
        "margin_v": 55,
        # Persistent glow on active word (no settle — stays glowing)
        "animate_active": True,
        "animate_flash_color": _COLOR_NEON_GREEN,
        "animate_glow_blur": 3,
        "animate_pop_scale": 100,
        "animate_pop_duration": 100,
        "animate_settle_duration": 9999,  # effectively permanent glow
    },
    "cleanboss": {
        "fontname": "DejaVu Sans",
        "fontsize": 18,
        "primary_color": _COLOR_LIGHT_GRAY,
        "highlight_color": _COLOR_WHITE,
        "outline_color": _COLOR_SOFT_BLACK,
        "back_color": "&H00000000&",
        "bold": 0,
        "outline": 1,
        "shadow": 2,
        "alignment": 2,
        "margin_v": 60,
        # Subtle scale bump, no flash
        "animate_active": True,
        "animate_flash_color": _COLOR_WHITE,
        "animate_glow_blur": 0,
        "animate_pop_scale": 106,
        "animate_pop_duration": 100,
        "animate_settle_duration": 120,
    },
    "staccato": {
        "fontname": "DejaVu Sans",
        "fontsize": 20,
        "primary_color": _COLOR_WHITE,
        "highlight_color": _COLOR_WHITE,  # only word shown, always white
        "outline_color": _COLOR_BLACK,
        "back_color": "&H00000000&",
        "bold": -1,
        "outline": 3,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 55,
        # Aggressive punch-in: 70% → 115% → 100% in 80ms
        "animate_active": True,
        "animate_flash_color": _COLOR_WHITE,
        "animate_glow_blur": 1,
        "animate_pop_scale": 115,
        "animate_pop_duration": 50,
        "animate_settle_duration": 30,
        # Special: only show active word (staccato mode)
        "staccato_mode": True,
    },
    "serenity": {
        "fontname": "DejaVu Sans",
        "fontsize": 17,
        "primary_color": _COLOR_LAVENDER,
        "highlight_color": _COLOR_HOT_PINK,
        "outline_color": "&H00000000&",  # no hard outline
        "back_color": "&H00000000&",
        "bold": 0,
        "outline": 0,
        "shadow": 3,
        "alignment": 2,
        "margin_v": 60,
        # Soft glow on active, gentle color transition
        "animate_active": True,
        "animate_flash_color": _COLOR_HOT_PINK,
        "animate_glow_blur": 2,
        "animate_pop_scale": 104,
        "animate_pop_duration": 180,
        "animate_settle_duration": 200,
    },
}


def _format_ass_time(seconds: float) -> str:
    """Convert seconds → ASS timestamp ``H:MM:SS.cc`` (centiseconds)."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape_ass_text(text: str) -> str:
    """Escape characters that have special meaning in ASS dialogue."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def generate_dynamic_ass(
    word_level_data: list[dict],
    output_ass_path: str,
    style_name: str = "hormozi_dynamic",
    video_width: int = 1080,
    video_height: int = 1920,
    max_words_per_line: int = 5,
) -> str:
    """
    Generate an .ASS subtitle file with per-word color highlighting.

    For each word chunk, a separate dialogue event is emitted for every word's
    active time span.  The full chunk text is shown, but the currently-spoken
    word gets the highlight color.

    Args:
        word_level_data: ``[{"word": "Hello", "start": 0.1, "end": 0.5}, ...]``
        output_ass_path: Where to write the .ASS file.
        style_name: ``"hormozi"`` or ``"classic"``.
        video_width: Target video width (for PlayResX).
        video_height: Target video height (for PlayResY).
        max_words_per_line: Words per caption group.

    Returns:
        The path to the written .ASS file.
    """
    preset = STYLE_PRESETS.get(style_name, STYLE_PRESETS["hormozi_dynamic"])

    # Scale fontsize relative to video height (preset tuned for 384px PlayRes)
    play_res_x = 384 * video_width // video_height  # keep aspect ratio
    play_res_y = 384
    fontsize = preset["fontsize"]
    outline = preset["outline"]
    shadow = preset["shadow"]
    margin_v = preset["margin_v"]

    # ── Header ────────────────────────────────────────────────────────────
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{preset['fontname']},{fontsize},"
        f"{preset['primary_color']},{preset['primary_color']},"
        f"{preset['outline_color']},{preset['back_color']},"
        f"{preset['bold']},0,0,0,100,100,0,0,1,{outline},{shadow},"
        f"{preset['alignment']},10,10,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    # ── Group words into chunks ───────────────────────────────────────────
    groups = _group_words_for_ass(word_level_data, max_words_per_line)

    # ── Build dialogue events ─────────────────────────────────────────────
    highlight_color = preset["highlight_color"]
    primary_color = preset["primary_color"]
    has_highlight = highlight_color != primary_color

    events: list[str] = []

    for group in groups:
        words_in_group = [w["word"] for w in group]

        for active_idx, active_word_data in enumerate(group):
            w_start = active_word_data["start"]
            w_end = active_word_data["end"]

            if w_end <= w_start:
                continue

            # Build the display line with style-specific logic
            parts: list[str] = []

            if style_name == "typewriter":
                # ── Typewriter: true per-letter reveal ───────────────────
                # Create one Dialogue event per character so each letter
                # appears individually, accumulating as the word is spoken.
                word_raw = words_in_group[active_idx]
                num_chars = max(1, len(word_raw))
                duration = max(0.001, w_end - w_start)
                time_per_char = duration / num_chars

                # Prefix: all already-typed words from earlier in the group
                past_parts = [
                    _escape_ass_text(words_in_group[j]) for j in range(active_idx)
                ]
                prefix = " ".join(past_parts) + (" " if past_parts else "")

                # Extend the last char's display to bridge the gap before the
                # next word starts (keeps text visible between words).
                if active_idx < len(group) - 1:
                    effective_end = max(w_end, group[active_idx + 1]["start"])
                else:
                    effective_end = w_end

                for char_idx in range(num_chars):
                    char_start = w_start + char_idx * time_per_char
                    char_end = (
                        effective_end
                        if char_idx == num_chars - 1
                        else w_start + (char_idx + 1) * time_per_char
                    )
                    if char_end <= char_start:
                        continue
                    typed = _escape_ass_text(word_raw[: char_idx + 1])
                    events.append(
                        f"Dialogue: 0,"
                        f"{_format_ass_time(char_start)},"
                        f"{_format_ass_time(char_end)},"
                        f"Default,,0,0,0,,"
                        f"{prefix}{typed}"
                    )
                continue  # skip the shared single-event append below

            else:
                # ── Original highlight / animated path ───────────────────
                animate = preset.get("animate_active", False)
                staccato = preset.get("staccato_mode", False)

                for j, word_text in enumerate(words_in_group):
                    escaped = _escape_ass_text(word_text.upper())
                    is_active = (j == active_idx)

                    if staccato:
                        # Staccato: ONLY render active word (punch-in animation)
                        if not is_active:
                            continue
                        # Active word with animation
                        if animate:
                            gb = preset.get("animate_glow_blur", 1)
                            ps = preset.get("animate_pop_scale", 115)
                            pd = preset.get("animate_pop_duration", 50)
                            sd = preset.get("animate_settle_duration", 30)
                            ov = (
                                f"{{\\c{primary_color}\\blur{gb}\\fscx70\\fscy70"
                                f"\\t(0,{pd},\\c{primary_color}\\blur0\\fscx{ps}\\fscy{ps})"
                                f"\\t({pd},{pd + sd},\\fscx100\\fscy100)}}"
                            )
                            parts.append(f"{ov}{escaped}{{\\blur0\\fscx100\\fscy100}}")
                        else:
                            parts.append(escaped)

                    elif has_highlight and is_active:
                        # Initial color: flash color if animated, else highlight
                        if animate and "animate_flash_color" in preset:
                            init_color = preset["animate_flash_color"]
                        else:
                            init_color = highlight_color

                        ov_on = f"\\c{init_color}"
                        ov_off = f"\\c{primary_color}"
                        if "highlight_outline_color" in preset:
                            ov_on += f"\\3c{preset['highlight_outline_color']}"
                            ov_off += f"\\3c{preset['outline_color']}"
                        if "highlight_outline" in preset:
                            ov_on += f"\\bord{preset['highlight_outline']}"
                            ov_off += f"\\bord{outline}"
                        if "highlight_shadow" in preset:
                            ov_on += f"\\shad{preset['highlight_shadow']}"
                            ov_off += f"\\shad{shadow}"
                        if "highlight_back_color" in preset:
                            ov_on += f"\\4c{preset['highlight_back_color']}"
                            ov_off += f"\\4c{preset['back_color']}"

                        # Animated pop: blur-glow + scale-pop + color-flash
                        if animate:
                            gb = preset.get("animate_glow_blur", 2)
                            ps = preset.get("animate_pop_scale", 108)
                            pd = preset.get("animate_pop_duration", 100)
                            sd = preset.get("animate_settle_duration", 80)
                            # Initial state: blurred + shrunk
                            ov_on += f"\\blur{gb}\\fscx90\\fscy90"
                            # Pop up: color transition + sharpen + overshoot scale
                            ov_on += (
                                f"\\t(0,{pd},"
                                f"\\c{highlight_color}\\blur0"
                                f"\\fscx{ps}\\fscy{ps})"
                            )
                            # Settle back to 100 %
                            ov_on += (
                                f"\\t({pd},{pd + sd},"
                                f"\\fscx100\\fscy100)"
                            )
                            ov_off += "\\blur0\\fscx100\\fscy100"

                        parts.append(f"{{{ov_on}}}{escaped}{{{ov_off}}}")
                    else:
                        parts.append(escaped)

            line_text = " ".join(parts)

            event = (
                f"Dialogue: 0,"
                f"{_format_ass_time(w_start)},"
                f"{_format_ass_time(w_end)},"
                f"Default,,0,0,0,,"
                f"{line_text}"
            )
            events.append(event)

    # ── Write file ────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(output_ass_path) or ".", exist_ok=True)
    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    logger.info(f"Generated ASS subtitle: {output_ass_path} ({len(events)} events)")
    return output_ass_path


def _group_words_for_ass(
    words: list[dict],
    group_size: int = 5,
    gap_threshold: float = 0.4,
) -> list[list[dict]]:
    """
    Group words into display chunks.

    A new chunk starts when:
    - ``group_size`` words reached, OR
    - timing gap > ``gap_threshold`` between consecutive words.
    """
    if not words:
        return []

    groups: list[list[dict]] = []
    current: list[dict] = [words[0]]

    for i in range(1, len(words)):
        prev_end = words[i - 1].get("end", 0)
        curr_start = words[i].get("start", 0)
        gap = curr_start - prev_end

        if len(current) >= group_size or gap > gap_threshold:
            groups.append(current)
            current = [words[i]]
        else:
            current.append(words[i])

    if current:
        groups.append(current)

    return groups


def burn_ass_subtitles(
    input_video_path: str,
    ass_path: str,
    output_video_path: str,
    cq: int = 18,
    preset: str = "p4",
    bitrate: str | None = None,
) -> str:
    """
    Burn an .ASS subtitle file into a video using FFmpeg's ``ass`` filter.

    Uses ``libx264`` (CPU). In CPU mode, CRF is used.

    Args:
        input_video_path: Source video (already cropped/resized).
        ass_path: Path to the .ASS file.
        output_video_path: Output destination.
        cq: Quality target — maps to ``-cq`` (NVENC) or ``-crf`` (x264).
            Lower values = better quality.
        preset: Encoder preset.  NVENC: ``p1``–``p7`` (p4 = balanced).
            Ignored when GPU is unavailable (``medium`` is used instead).
        bitrate: Optional max bitrate cap (e.g. ``"5M"``).

    Returns:
        Path to the output video.
    """
    # Escape special chars in the path for FFmpeg filter syntax
    # FFmpeg ass filter needs : \ ' escaped
    escaped_ass = ass_path.replace("\\", "\\\\\\\\")
    escaped_ass = escaped_ass.replace(":", "\\:")
    escaped_ass = escaped_ass.replace("'", "'\\''")

    vf = f"ass='{escaped_ass}'"

    if _GPU_AVAILABLE:
        # NVENC: hardware-accelerated encoding with VBR + CQ quality target
        cmd = [
            "ffmpeg",
            "-hwaccel", "cuda",
            "-i", input_video_path,
            "-vf", vf,
            "-c:v", "h264_nvenc",
            "-rc:v", "vbr",
            "-cq", str(cq),
            "-preset", preset,
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
        ]
        if bitrate:
            bufsize = str(int(bitrate.rstrip("M")) * 2) + "M"
            cmd.extend(["-maxrate", bitrate, "-bufsize", bufsize])
    else:
        # CPU fallback: libx264 with CRF
        cmd = [
            "ffmpeg",
            "-i", input_video_path,
            "-vf", vf,
            "-c:v", "libx264",
            "-crf", str(cq),
            "-preset", "medium",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
        ]
        if bitrate:
            bufsize = str(int(bitrate.rstrip("M")) * 2) + "M"
            cmd.extend(["-maxrate", bitrate, "-bufsize", bufsize])

    cmd.extend(["-y", output_video_path])

    logger.info(f"Burning ASS subtitles ({'GPU' if _GPU_AVAILABLE else 'CPU'}): {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error(f"FFmpeg ASS burn failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
        raise RuntimeError(f"FFmpeg subtitle burn failed: {result.stderr[-500:]}")

    logger.info(f"ASS subtitle burn complete: {output_video_path}")
    return output_video_path
