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
    "free": {"min": 30, "max": 60},
    "pro": {"min": 30, "max": 60},
    "enterprise": {"min": 30, "max": 90},
}

# ---------------------------------------------------------------------------
# Pydantic Model
# ---------------------------------------------------------------------------

class ViralClip(BaseModel):
    title: str = Field(..., min_length=1)
    hook_text: str = Field(default="", description="Kalimat hook pembuka klip")
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
# Dynamic Prompt Generator
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """Lu adalah seorang **Expert Content Director** dan **Video Editor** untuk platform short-form (TikTok, Reels, Shorts).
Tugas lu adalah menganalisis transkrip video YouTube beserta timestamp-nya, dan mengekstrak momen-momen emas terbaik untuk dijadikan klip pendek.

User telah meminta lu untuk HANYA mencari klip yang masuk dalam kategori berikut: [{kategori_diminta}].

### DEFINISI KATEGORI:
- Edukasi: Momen yang memberikan wawasan baru, tips praktis, fakta menarik, atau penjelasan suatu konsep dengan cara yang mudah dipahami.
- Lucu: Momen komedi, candaan (jokes), reaksi kocak, atau situasi absurd yang menghibur.
- Urgent dan Panas: Pembahasan yang emosional, perdebatan tajam, isu trending/kontroversial, atau opini yang memicu diskusi (hook yang provokatif).

### ATURAN MUTLAK (WAJIB DIIKUTI):
1. DURASI HARGA MATI: Durasi klip WAJIB di antara {min_duration} hingga {max_duration} detik. Jika sebuah ide atau argumen memakan waktu lebih dari {max_duration} detik, TINGGALKAN momen tersebut dan cari momen lain. JANGAN melanggar batas maksimal durasi.
2. ANTI-INTRO & ANTI-BASA-BASI: ABAIKAN lagu intro, bumper video, sapaan awal ("Welcome back to my channel", "Halo guys"), atau promosi sponsor. START_TIME harus dimulai TEPAT saat pembicara mengucapkan kalimat "Hook" pertama yang berbobot.
3. KONTEKS HARUS UTUH & SEMANTIC CLOSURE: Awalan klip harus jelas, dan akhiran klip HARUS pemikiran yang tuntas. Geser timestamp end_time ke titik terdekat di mana pembicara selesai menjelaskan poin tersebut (titik/jeda napas), TETAPI tetap harus mematuhi aturan nomor 1 (maksimal {max_duration} detik).
4. HOOK KUAT: Pastikan 3-5 detik pertama memiliki kalimat pancingan (hook) yang provokatif atau mengundang rasa penasaran.
5. start_time dan end_time HARUS merujuk pada timestamp yang ada di transkrip secara presisi.
6. Klip harus bisa berdiri sendiri (stand-alone) tanpa perlu konteks tambahan dari video asli.
7. Klip TIDAK BOLEH saling tumpang tindih (overlap).
8. Berikan `virality_score` antara 1-100. Nilai berdasarkan kekuatan hook dan seberapa padat informasinya tanpa bertele-tele.
9. HANYA kembalikan klip dengan virality_score >= 80.
10. Kembalikan MAKSIMAL 15 klip. Jika ada lebih, pilih 15 dengan skor tertinggi.

### CATATAN KONDISI TRANSKRIP:
Transkrip JSON yang lu terima dihasilkan oleh mesin Speech-to-Text (Whisper). Akan ada banyak kesalahan ejaan (typo), salah tangkap bahasa gaul, atau singkatan yang ditulis berdasarkan fonetiknya (contoh: "FYP" menjadi "evyepi").
TUGAS LU: Gunakan nalar semantik untuk memahami konteks asli dari kalimat yang typo tersebut saat mencari Hook dan momen klip. Jangan sampai momen emas terlewat hanya karena typo mesin.

### PENTING TENTANG END_TIME:
Karena lu hanya membaca teks, lu harus sangat peka terhadap 'Semantic Closure' (Akhir Pemikiran).
Jangan pernah memotong (end_time) saat pembicara sedang menggunakan kata sambung (dan, tetapi, karena, sehingga) atau di tengah penyampaian ide.
Pastikan kata terakhir di batas end_time adalah sebuah kesimpulan, akhir dari sebuah poin, atau punchline yang tuntas. Lebih baik durasi klip lebih panjang 3 detik daripada memotong kalimat secara prematur."

DURASI VIDEO: {video_duration} detik
TRANSKRIP:
{transcript_text}

FORMAT OUTPUT — JSON array dengan field berikut untuk setiap klip:
- "title": Judul pendek yang catchy dan click-worthy (Bahasa Indonesia).
- "hook_text": Kalimat hook pembuka klip yang membuat penonton penasaran.
- "start_time": Waktu mulai dalam detik (angka desimal).
- "end_time": Waktu selesai dalam detik (angka desimal).
- "virality_score": Skor viralitas 1-100 (integer).
- "reasoning": Satu kalimat singkat kenapa klip ini viral (Bahasa Indonesia)."""

# Category label mapping for prompt injection
_CATEGORY_LABELS = {
    "edukasi": "Edukasi",
    "lucu": "Lucu",
    "urgent_panas": "Urgent dan Panas",
}


def generate_gemini_prompt(
    user_tier: str,
    custom_min: int = 30,
    custom_max: int = 60,
    categories: list[str] | None = None,
) -> str:
    """
    Return the Gemini prompt template with duration constraints based on tier
    and category filtering injected.

    - free: hardcoded 15-30s
    - pro/enterprise: uses custom_min / custom_max (defaults 30-60s)
    """
    if user_tier == "free":
        min_dur = TIER_DURATIONS["free"]["min"]
        max_dur = TIER_DURATIONS["free"]["max"]
    else:
        min_dur = max(1, custom_min)
        max_dur = max(min_dur + 1, custom_max)

    # Build category string for prompt
    if categories:
        kategori_diminta = ", ".join(
            _CATEGORY_LABELS.get(c, c) for c in categories
        )
    else:
        kategori_diminta = ", ".join(_CATEGORY_LABELS.values())

    # Return the template with duration and category placeholders filled,
    # but {video_duration} and {transcript_text} still open.
    return (
        _PROMPT_TEMPLATE
        .replace("{min_duration}", str(min_dur))
        .replace("{max_duration}", str(max_dur))
        .replace("{kategori_diminta}", kategori_diminta)
    )


CLIP_SCHEMA = types.Schema(
    type=types.Type.ARRAY,
    items=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "title": types.Schema(type=types.Type.STRING, description="Judul pendek yang catchy"),
            "hook_text": types.Schema(type=types.Type.STRING, description="Kalimat hook pembuka klip"),
            "start_time": types.Schema(type=types.Type.NUMBER, description="Waktu mulai dalam detik"),
            "end_time": types.Schema(type=types.Type.NUMBER, description="Waktu selesai dalam detik"),
            "virality_score": types.Schema(type=types.Type.INTEGER, description="Skor viralitas 1-100"),
            "reasoning": types.Schema(type=types.Type.STRING, description="Alasan kenapa klip ini viral"),
        },
        required=["title", "hook_text", "start_time", "end_time", "virality_score", "reasoning"],
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
    custom_min: int | None = None,
    custom_max: int | None = None,
    categories: list[str] | None = None,
) -> list[dict]:
    """
    Backward-compatible wrapper for process_video_task.
    Calls extract_and_filter_clips and converts to plain dicts.
    """
    clips = extract_and_filter_clips(
        transcript_text,
        video_duration=video_duration,
        user_tier=user_tier,
        custom_min=custom_min,
        custom_max=custom_max,
        categories=categories,
    )
    return [c.model_dump() for c in clips]


def extract_and_filter_clips(
    transcript_text: str,
    video_duration: float = 0,
    user_tier: str = "free",
    custom_min: int | None = None,
    custom_max: int | None = None,
    categories: list[str] | None = None,
) -> list[ViralClip]:
    """
    Full pipeline: prompt Gemini → parse → validate → smart-pad → filter → cap.

    Args:
        transcript_text: Formatted transcript with timestamps.
        video_duration: Total video duration in seconds.
        user_tier: "free", "pro", or "enterprise".
        custom_min: Override minimum clip duration (pro/enterprise only).
        custom_max: Override maximum clip duration (pro/enterprise only).

    Returns:
        List of ViralClip objects (score >= 80, padded, max 15 items).
    """
    # Resolve duration constraints
    tier_cfg = TIER_DURATIONS.get(user_tier, TIER_DURATIONS["free"])
    min_dur = custom_min if (custom_min is not None and user_tier != "free") else tier_cfg["min"]
    max_dur = custom_max if (custom_max is not None and user_tier != "free") else tier_cfg["max"]

    # --- Build prompt ---
    prompt_template = generate_gemini_prompt(user_tier, min_dur, max_dur, categories=categories)
    dur_str = f"{video_duration:.1f}" if video_duration else "unknown"
    prompt = prompt_template.format(transcript_text=transcript_text, video_duration=dur_str)

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

    models_to_try = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
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
