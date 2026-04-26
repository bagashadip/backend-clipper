import json
import logging
import time
from pydantic import BaseModel, Field, field_validator
from google import genai
from google.genai import types
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_CLIPS = 15
MIN_VIRALITY_SCORE = 80

# Tier-based duration limits
TIER_DURATIONS: dict[str, dict[str, int]] = {
    "free":       {"min": 30, "max": 60},
    "pro":        {"min": 30, "max": 60},
    "enterprise": {"min": 30, "max": 90},
}

# ---------------------------------------------------------------------------
# Pydantic Model
# ---------------------------------------------------------------------------

class ViralClip(BaseModel):
    title: str = Field(..., min_length=1)
    start_time: float = Field(..., ge=0)
    end_time: float = Field(..., gt=0)
    virality_score: int = Field(..., ge=1, le=100)
    reasoning: str = Field(default="")

    @field_validator("end_time")
    @classmethod
    def end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start_time", 0)
        if v <= start:
            raise ValueError("end_time must be greater than start_time")
        return v

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """Kamu adalah seorang **Viral Content Strategist** dan **Senior Video Editor** berpengalaman untuk platform short-form (TikTok, Instagram Reels, YouTube Shorts).

Tugasmu: Analisis transkrip video YouTube berikut dan ekstrak momen-momen terbaik yang berpotensi viral sebagai klip pendek.

---
### ATURAN WAJIB (HARUS DIPATUHI SEMUA):

1. **DURASI**: Setiap klip HARUS berdurasi antara {min_duration}–{max_duration} detik. Tidak kurang, tidak lebih. Ini harga mati.

2. **MULAI DARI HOOK**: `start_time` harus tepat di momen pembicara mengucapkan kalimat pertama yang berbobot — bukan intro, bukan sapaan, bukan sponsor. Langsung inti.

3. **AKHIRI DENGAN TUNTAS**: `end_time` harus di titik pembicara benar-benar menyelesaikan satu pikiran/poin — bukan di tengah kalimat, bukan sebelum kesimpulan. Lebih baik 2–3 detik lebih panjang daripada memotong di tengah.

4. **BERDIRI SENDIRI**: Klip harus dapat dipahami tanpa konteks video asli.

5. **TIDAK OVERLAP**: Klip tidak boleh saling tumpang tindih satu sama lain.

6. **SKOR MINIMAL 80**: Hanya kembalikan klip dengan `virality_score` ≥ 80. Jika tidak ada yang memenuhi, jangan kembalikan apa-apa.

7. **MAKSIMAL {max_clips} KLIP**: Jika lebih, pilih yang skor tertinggi.

8. **TIMESTAMP PRESISI**: `start_time` dan `end_time` harus merujuk pada timestamp yang benar-benar ada di transkrip.

---
### KRITERIA KLIP YANG VIRAL:

Pilih momen yang mengandung salah satu atau lebih dari ini:
- **Hook provokatif**: Kalimat pembuka yang langsung memancing rasa penasaran atau emosi
- **Insight padat**: Fakta mengejutkan, tips praktis, atau perspektif unik yang disampaikan ringkas
- **Momen emosional**: Konflik, ketegangan, humor, atau momen "aha" yang autentik
- **Cerita mini**: Arc yang utuh — ada setup, konflik/inti, resolusi — dalam durasi pendek

---
### TENTANG TRANSKRIP:

Transkrip dihasilkan oleh mesin Speech-to-Text (Whisper). Mungkin ada typo, kesalahan ejaan gaul, atau kata-kata yang salah ditangkap. Gunakan nalar semantik untuk memahami konteks sesungguhnya.

---
DURASI VIDEO: {video_duration} detik
TRANSKRIP:
{transcript_text}

---
Kembalikan JSON array. Setiap item:
- "title": Judul pendek yang catchy (Bahasa Indonesia, maks 10 kata)
- "start_time": Waktu mulai (detik, desimal)
- "end_time": Waktu selesai (detik, desimal)
- "virality_score": 1–100 (integer)
- "reasoning": Satu kalimat — kenapa ini viral (Bahasa Indonesia)"""


def _build_prompt(user_tier: str, video_duration: float, transcript_text: str) -> str:
    tier_cfg = TIER_DURATIONS.get(user_tier, TIER_DURATIONS["free"])
    min_dur = tier_cfg["min"]
    max_dur = tier_cfg["max"]
    dur_str = f"{video_duration:.1f}" if video_duration else "unknown"
    return (
        _PROMPT_TEMPLATE
        .replace("{min_duration}", str(min_dur))
        .replace("{max_duration}", str(max_dur))
        .replace("{max_clips}", str(MAX_CLIPS))
        .replace("{video_duration}", dur_str)
        .replace("{transcript_text}", transcript_text)
    )


CLIP_SCHEMA = types.Schema(
    type=types.Type.ARRAY,
    items=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "title":          types.Schema(type=types.Type.STRING,  description="Judul pendek yang catchy"),
            "start_time":     types.Schema(type=types.Type.NUMBER,  description="Waktu mulai dalam detik"),
            "end_time":       types.Schema(type=types.Type.NUMBER,  description="Waktu selesai dalam detik"),
            "virality_score": types.Schema(type=types.Type.INTEGER, description="Skor viralitas 1-100"),
            "reasoning":      types.Schema(type=types.Type.STRING,  description="Alasan kenapa klip ini viral"),
        },
        required=["title", "start_time", "end_time", "virality_score", "reasoning"],
    ),
)

# ---------------------------------------------------------------------------
# Smart Padding Algorithm
# ---------------------------------------------------------------------------

def apply_smart_padding(
    clip_start: float,
    clip_end: float,
    target_min: int,
    video_total_duration: float,
) -> tuple[float, float]:
    """
    Expand a clip symmetrically to meet `target_min` duration.

    If the clip is already >= target_min, return unchanged.
    Adds half the deficit before start and half after end.
    If one side hits a boundary (0 or video_total_duration),
    the remaining deficit shifts to the other side.

    Returns:
        (new_start, new_end) — guaranteed within [0, video_total_duration].
    """
    current_dur = clip_end - clip_start
    if current_dur >= target_min:
        return (clip_start, clip_end)

    deficit = target_min - current_dur
    half = deficit / 2.0

    new_start = clip_start - half
    new_end = clip_end + half

    # --- Boundary correction ---
    if new_start < 0.0:
        overshoot = -new_start
        new_start = 0.0
        new_end += overshoot  # shift remainder forward

    if new_end > video_total_duration:
        overshoot = new_end - video_total_duration
        new_end = video_total_duration
        new_start = max(0.0, new_start - overshoot)  # shift remainder backward

    # Final clamp (safety)
    new_start = max(0.0, round(new_start, 3))
    new_end = min(video_total_duration, round(new_end, 3))

    return (new_start, new_end)

# ---------------------------------------------------------------------------
# Transcript Formatter
# ---------------------------------------------------------------------------

def format_whisper_to_text(whisper_words: list[dict]) -> str:
    """
    Group Whisper word-level timestamps into readable sentences with time markers.

    Input:  [{"word": "Halo", "start": 0.1, "end": 0.5}, ...]
    Output: "[00:00.100 -> 00:05.200] Halo semuanya selamat datang."
    """
    if not whisper_words:
        return ""

    def _fmt(seconds: float) -> str:
        m, s = divmod(seconds, 60)
        return f"{int(m):02d}:{s:06.3f}"

    SENTENCE_ENDERS = {".", "!", "?", "。"}
    MAX_GROUP_SEC = 8.0
    GAP_THRESHOLD = 1.5

    lines: list[str] = []
    buf_words: list[str] = []
    buf_start: float = whisper_words[0]["start"]
    buf_end: float = whisper_words[0]["end"]

    for w in whisper_words:
        word = w["word"].strip()
        if not word:
            continue

        gap = w["start"] - buf_end
        span = w["end"] - buf_start
        ends_sentence = any(word.endswith(p) for p in SENTENCE_ENDERS)

        # Flush current buffer on sentence end, long gap, or max duration
        if buf_words and (ends_sentence or gap > GAP_THRESHOLD or span > MAX_GROUP_SEC):
            if ends_sentence:
                buf_words.append(word)
                buf_end = w["end"]
            lines.append(f"[{_fmt(buf_start)} -> {_fmt(buf_end)}] {' '.join(buf_words)}")
            if ends_sentence:
                buf_words = []
                buf_start = w["end"]
                buf_end = w["end"]
                continue
            else:
                buf_words = []
                buf_start = w["start"]
                buf_end = w["end"]

        buf_words.append(word)
        buf_end = w["end"]

    # Flush remaining
    if buf_words:
        lines.append(f"[{_fmt(buf_start)} -> {_fmt(buf_end)}] {' '.join(buf_words)}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main Extraction Function
# ---------------------------------------------------------------------------

def select_viral_clips(
    transcript_text: str,
    video_duration: float = 0,
    user_tier: str = "free",
) -> list[dict]:
    """
    Backward-compatible wrapper for process_video_task.
    Calls extract_and_filter_clips and converts to plain dicts.
    """
    clips = extract_and_filter_clips(
        transcript_text,
        video_duration=video_duration,
        user_tier=user_tier,
    )
    return [c.model_dump() for c in clips]


def extract_and_filter_clips(
    transcript_text: str,
    video_duration: float = 0,
    user_tier: str = "free",
) -> list[ViralClip]:
    """
    Full pipeline: prompt Gemini → parse → validate → smart-pad → filter → cap.

    Returns:
        List of ViralClip objects (score >= 80, padded to min_dur, max 15 items).
    """
    tier_cfg = TIER_DURATIONS.get(user_tier, TIER_DURATIONS["free"])
    min_dur = tier_cfg["min"]
    max_dur = tier_cfg["max"]

    # --- Build prompt ---
    prompt = _build_prompt(user_tier, video_duration, transcript_text)

    # --- Call Gemini ---
    client = genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(timeout=180_000),
    )

    config = types.GenerateContentConfig(
        temperature=0.7,
        max_output_tokens=8192,
        response_mime_type="application/json",
        response_schema=CLIP_SCHEMA,
    )

    logger.info(f"Sending transcript to Gemini (tier={user_tier}, duration={min_dur}-{max_dur}s)")

    models_to_try = ["gemini-1.5-pro", "gemini-2.5-flash"]
    raw_clips: list[dict] | None = None

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                logger.info(f"Trying model {model_name} (attempt {attempt+1}/3)")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )

                # Try structured parse first, fall back to raw JSON text
                if response.parsed:
                    raw_clips = response.parsed
                elif response.text:
                    raw_clips = json.loads(response.text)

                if raw_clips:
                    logger.info(f"Got {len(raw_clips)} raw clips from {model_name}")
                    break

            except Exception as e:
                err_str = str(e)
                if ("503" in err_str or "429" in err_str or "UNAVAILABLE" in err_str
                        or "RESOURCE_EXHAUSTED" in err_str or "timed out" in err_str) and attempt < 2:
                    wait = 2 ** attempt * 5
                    logger.warning(f"{model_name} transient error (attempt {attempt+1}/3), retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    logger.warning(f"{model_name} failed: {e}")
                    break
        if raw_clips:
            break

    if not raw_clips:
        raise RuntimeError("All Gemini models failed for clip selection")

    # --- Step 1: Pydantic validation ---
    validated: list[ViralClip] = []
    for i, item in enumerate(raw_clips):
        try:
            clip = ViralClip(**item) if isinstance(item, dict) else ViralClip(**dict(item))
            validated.append(clip)
        except Exception as e:
            logger.warning(f"Skipping invalid clip at index {i}: {e}")

    # --- Step 2: Enforce minimum virality score ---
    validated = [c for c in validated if c.virality_score >= MIN_VIRALITY_SCORE]

    # --- Step 3: Drop clips beyond video duration ---
    if video_duration > 0:
        before = len(validated)
        validated = [c for c in validated if c.start_time < video_duration and c.end_time <= video_duration + 1]
        dropped = before - len(validated)
        if dropped:
            logger.warning(f"Dropped {dropped} clips beyond video duration ({video_duration:.1f}s)")

    # --- Step 4: Smart Padding for clips below min_dur ---
    if video_duration > 0:
        padded: list[ViralClip] = []
        for clip in validated:
            if clip.duration < min_dur:
                new_start, new_end = apply_smart_padding(
                    clip.start_time, clip.end_time, min_dur, video_duration,
                )
                logger.info(
                    f"Padded '{clip.title}': {clip.start_time:.1f}-{clip.end_time:.1f}s "
                    f"({clip.duration:.1f}s) → {new_start:.1f}-{new_end:.1f}s ({new_end - new_start:.1f}s)"
                )
                clip = clip.model_copy(update={"start_time": new_start, "end_time": new_end})
            padded.append(clip)
        validated = padded

    # --- Step 5: Failsafe — drop clips that STILL don't meet min_dur ---
    before = len(validated)
    validated = [c for c in validated if c.duration >= min_dur - 0.5]  # 0.5s tolerance
    dropped = before - len(validated)
    if dropped:
        logger.warning(f"Dropped {dropped} clips still below minimum duration ({min_dur}s) after padding")

    # --- Step 6: Sort by score descending, cap at MAX_CLIPS ---
    validated.sort(key=lambda c: c.virality_score, reverse=True)
    validated = validated[:MAX_CLIPS]

    if not validated:
        raise ValueError(
            f"No clips passed validation (score >= {MIN_VIRALITY_SCORE}, dur >= {min_dur}s). "
            f"Raw response had {len(raw_clips)} items."
        )

    logger.info(
        f"Selected {len(validated)} clips "
        f"(scores: {[c.virality_score for c in validated]}, "
        f"durations: {[round(c.duration, 1) for c in validated]})"
    )
    return validated
