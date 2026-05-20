import json
import logging
import re
import time
from pydantic import BaseModel, Field, field_validator
from anthropic import Anthropic
from openai import OpenAI
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_CLIPS = 15
MIN_VIRALITY_SCORE = 80


# ── Category catalog (Indonesian copy preserved from original prompt) ────
# Each definition is opt-in: only the user's `selected_categories` are
# injected into the prompt to save tokens on Claude/DeepSeek calls.

CATEGORY_DEFINITIONS: dict[str, str] = {
    "humor": (
        "**1. 😂 LUCU / HUMOR (Skor 88-98)**\n"
        "- Punchline yang delivered dengan timing pas\n"
        "- Momen kocak, kesalahan lucu, atau reaksi tak terduga\n"
        "- Sarkasme cerdas, roasts yang menghibur\n"
        "- Storytelling komedi dengan setup → punchline\n"
        "- *Ciri transkrip*: nada cerita berubah lucu, ada kata-kata "
        "ekspresif, diikuti tawa atau reaksi"
    ),
    "shock": (
        "**2. 😱 KAGET / SHOCK / PLOT TWIST (Skor 90-100)**\n"
        "- Fakta yang membalikkan ekspektasi total\n"
        "- Pengakuan mengejutkan atau rahasia yang dibeberkan\n"
        "- Statistik atau data yang bikin \"WHAT?!\"\n"
        "- Momen \"aku nggak nyangka...\" yang genuine\n"
        "- *Ciri transkrip*: nada bicara berubah drastis, jeda dramatis, "
        "kata-kata seperti \"ternyata...\", \"sebenarnya...\", \"yang mengejutkan...\""
    ),
    "controversy": (
        "**3. 🔥 KONTROVERSI / HOT TAKE / DEBAT (Skor 85-95)**\n"
        "- Opini tajam yang memancing diskusi\n"
        "- Kritik pedas terhadap industri/figur/tren\n"
        "- Pembelaan argumen yang kuat dan tak terduga\n"
        "- \"Unpopular opinion\" yang bikin orang komen\n"
        "- *Ciri transkrip*: kata-kata provokatif, konfrontasi, "
        "\"gua nggak setuju...\", \"masalahnya tuh...\""
    ),
    "emotional": (
        "**4. 😢 HARU / EMOSIONAL / STORYTELLING (Skor 84-92)**\n"
        "- Kisah perjuangan, kehilangan, atau pengorbanan\n"
        "- Momen vulnerable yang genuine dan menyentuh\n"
        "- Cerita yang membuat pendengar ikut merasakan\n"
        "- *Ciri transkrip*: nada melambat, suara bergetar, kata-kata "
        "personal \"waktu itu aku...\""
    ),
    "insight": (
        "**5. 💡 INSIGHT / MINDSET SHIFT / TIPS LANGKA (Skor 82-90)**\n"
        "- Cara pandang baru yang mengubah perspektif\n"
        "- Tips praktis yang jarang dibahas orang\n"
        "- \"Hack\" kehidupan atau pekerjaan yang brillian\n"
        "- Analogi cerdas yang membuat konsep rumit jadi sederhana\n"
        "- *Ciri transkrip*: \"kuncinya adalah...\", \"yang orang nggak sadar...\", "
        "\"satu hal yang...\""
    ),
    "motivation": (
        "**6. 🚀 MOTIVASI / INSPIRASI (Skor 80-88)**\n"
        "- Pesan pembangkit semangat yang tidak klise\n"
        "- Kisah sukses dengan pelajaran konkrit\n"
        "- \"Wake up call\" yang genuine\n"
        "- *Ciri transkrip*: nada bicara bersemangat, ajakan aksi, "
        "\"kalau aku bisa, kamu juga bisa\""
    ),
}

ALL_CATEGORIES = list(CATEGORY_DEFINITIONS.keys())


def _clean_and_parse_json(text: str | None) -> list | None:
    """
    Strip markdown code fences from a Gemini response and parse as JSON.
    Returns a list on success, None on any failure.
    """
    if not text:
        return None
    try:
        # Remove ```json ... ``` or ``` ... ``` wrappers
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
        # Gemini sometimes wraps the array in {"clips": [...]}
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return v
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(f"JSON parse failed: {exc} | snippet: {text[:300]}")
        return None

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

_PROMPT_TEMPLATE = """Kamu adalah seorang **Viral Content Strategist** dan **Senior Video Editor** dengan 10+ tahun pengalaman mengkurasi konten untuk TikTok, Instagram Reels, dan YouTube Shorts. Spesialisasimu adalah menemukan "momen emas" dalam video panjang yang bisa berdiri sendiri sebagai klip viral.

Tugasmu: Analisis transkrip video YouTube berikut dan ekstrak momen-momen terbaik **yang strictly cocok ke salah satu kategori berikut yang DIPILIH user**: {selected_category_labels}.

KLIP YANG TIDAK COCOK KE KATEGORI DI ATAS HARUS DIBUANG, sekalipun menarik.

---
### 🚨 PROTOKOL VERIFIKASI KALIMAT (DIBACA DULU SEBELUM BEKERJA)

Ini adalah aturan PALING PENTING. Setiap klip yang kamu hasilkan WAJIB melewati verifikasi ini:

**Sebelum menetapkan `end_time`, LAKUKAN 3 LANGKAH INI:**

1. **BUKA transkrip di timestamp `end_time` yang kamu usulkan.**
2. **BACA 3 kata terakhir sebelum timestamp itu.** Apakah membentuk akhir kalimat yang utuh? Kalimat utuh ditandai dengan: ada titik (.), tanda tanya (?), tanda seru (!), atau intonasi turun natural yang menandakan satu ide SELESAI.
3. **Jika BELUM selesai:** MAJUKAN `end_time` ke depan sampai kamu menemukan akhir kalimat yang SESUNGGUHNYA. Jika maju ke depan membuat durasi melebihi {max_duration} detik, maka MUNDURKAN `start_time` secukupnya agar klip tetap dalam batas durasi TANPA memotong kalimat.

**Prinsip utama: Lebih baik klip lebih pendek tapi KALIMAT SELESAI, daripada klip pas durasi tapi KALIMAT PUTUS. Ketuntasan kalimat adalah PRIORITAS TERTINGGI — di atas skor viralitas, di atas durasi ideal.**

---
### ATURAN WAJIB:

**A. STRUKTUR KLIP:**
1. **DURASI IDEAL**: Targetkan panjang klip di kisaran {min_duration}–{max_duration} detik. NAMUN, ada TOLERANSI. Jika kalimat pembicara belum selesai di detik ke-{max_duration}, kamu WAJIB meneruskan `end_time` beberapa detik lagi (melewati batas maksimal) sampai kalimat tersebut benar-benar titik/selesai. Kesempurnaan kalimat jauh lebih penting daripada batas durasi.
2. **START HARUS HOOK**: `start_time` tepat di momen pembicara mulai mengucapkan kalimat pertama yang berbobot. LEWATI: intro, sapaan "hai guys", "selamat datang kembali", basa-basi, sponsor, atau pengantar "di video kali ini".
3. **END HARUS TUNTAS (STORY ARC)**: `end_time` BUKAN sekadar titik kalimat biasa, tapi titik di mana KESIMPULAN atau PUNCHLINE dari topik tersebut benar-benar tersampaikan. 
   - Jika pembicara sedang menjelaskan sebuah konsep, berargumen, atau bercerita, JANGAN potong di tengah-tengah penjelasan. 
   - Manfaatkan durasi semaksimal mungkin (mendekati {max_duration} detik) jika memang cerita tersebut membutuhkannya agar konteksnya 100% utuh dari *Setup* hingga *Resolution*.
   - VERIFIKASI dengan protokol di atas untuk memastikan pemotongan terjadi tepat setelah kalimat kesimpulan selesai.
4. **BERDIRI SENDIRI**: Klip harus bisa dipahami orang yang TIDAK PERNAH menonton video aslinya. Jika ada referensi ke "tadi", "sebelumnya", "video kemarin", atau konteks yang hilang — jangan dipilih.

**B. FORMAT TEKNIS:**
5. **TIMESTAMP TOTAL DETIK (INTEGER)** — contoh: 1 menit 30 detik = 90, 2 menit 5 detik = 125. DILARANG: format "01:23" atau desimal "1.23".
6. **TIMESTAMP HARUS ADA DI TRANSKRIP** — jangan mengarang. `start_time` dan `end_time` harus bisa ditemukan di timestamp transkrip asli.
7. **TIDAK OVERLAP**: Dua klip tidak boleh saling tumpang tindih waktunya.

**C. KUALITAS:**
8. **SKOR MINIMAL 80**: Hanya klip dengan `virality_score` ≥ 80 yang dikembalikan. Tidak ada yang memenuhi? Kembalikan array kosong [].
9. **MAKSIMAL {max_clips} klip**: Urutkan dari skor tertinggi.

---
### 🎯 KATEGORI YANG DIIZINKAN (HANYA YANG DIPILIH USER):

{category_definitions}

---
### ❌ ANTI-POLA — HAL YANG TIDAK BOLEH DIPILIH:

Ini adalah momen-momen yang TERLIHAT seperti konten bagus tapi SEBENARNYA TIDAK:

- **🚫 Tidak cocok ke kategori yang dipilih**: SKIP, sekalipun menarik
- **🚫 Kalimat putus**: hook bagus tapi `end_time` di tengah kalimat (pelanggaran fatal)
- **🚫 Sponsor/iklan**: "video ini disponsori...", "link di deskripsi...", "pakai kode promo..."
- **🚫 Basa-basi**: "hai guys selamat datang...", "jangan lupa like subscribe...", "sebelum mulai..."
- **🚫 Call-to-action kosong**: "follow Instagram gua ya", "share ke temen-temen" — tanpa value
- **🚫 Cerita gantung**: "nanti gua ceritain di part 2", "kita bahas nanti" — tidak standalone
- **🚫 Pengulangan**: poin yang sama dengan klip lain, hanya wording berbeda
- **🚫 Di luar konteks**: referensi ke "tadi", "seperti yang gua bilang sebelumnya", "video kemarin"

---
### 📊 RUBRIK PENILAIAN `virality_score`:

| Skor | Kategori | Syarat |
|------|----------|--------|
| 95–100 | **BINTANG UTAMA** | Hook instant + emosi kuat + kalimat sempurna + shareable tanpa konteks |
| 88–94  | **SANGAT VIRAL** | Hook kuat + insight/humor berkesan + struktur final bagus |
| 80–87  | **POTENSI VIRAL** | Momen solid, konten menarik, tapi hook atau ending kurang sempurna |
| <80    | **TIDAK LOLOS** | Jangan dikembalikan |

**CATATAN PENTING**: Klip dengan kalimat yang TIDAK SELESAI di akhir OTOMATIS mendapat skor di bawah 80 — tidak peduli seberapa bagus hook-nya. Ketuntasan kalimat adalah syarat mutlak.

---
### 📝 TENTANG TRANSKRIP:

Transkrip dihasilkan oleh mesin Speech-to-Text. Mungkin ada:
- Typo dan salah eja (contoh: "gak" → "nggak", "aja" → "saja")
- Kata slang atau bahasa gaul yang ditangkap salah
- Tidak ada tanda baca (titik/koma) — kamu harus menyimpulkan akhir kalimat dari KONTEKS dan STRUKTUR BICARA

Gunakan NALAR SEMANTIK, bukan literal. Pahami maksud pembicara, bukan bunyi eksaknya.

---
DURASI VIDEO: {video_duration} detik
TRANSKRIP:
{transcript_text}

---
### ⚠️ FINAL SELF-CHECK (WAJIB DILAKUKAN SEBELUM OUTPUT):

Sebelum kamu mengeluarkan JSON, periksa SETIAP klip satu per satu:

1. Buka transkrip di `end_time` yang kamu pilih.
2. Baca kata TERAKHIR yang masuk ke dalam klip. Apakah kalimat itu SELESAI?
3. Jika kata terakhir adalah kata sambung (dan, tapi, karena, yang, untuk, jadi, maka, kalau, supaya, ketika) atau kata kerja tanpa objek — KALIMAT BELUM SELESAI. MAJUKAN `end_time` sampai kalimat tuntas.

**CONTOH SALAH (JANGAN DITIRU):**
```
end_time = 45 → kata terakhir: "karena" → ❌ KALIMAT PUTUS
end_time = 52 → kata terakhir: "untuk" → ❌ KALIMAT PUTUS  
end_time = 38 → kata terakhir: "yang" → ❌ KALIMAT PUTUS
```

**CONTOH BENAR:**
```
end_time = 47 → kata terakhir: "berhasil." → ✅ KALIMAT SELESAI
end_time = 55 → kata terakhir: "gitu." → ✅ KALIMAT SELESAI
end_time = 41 → kata terakhir: "banget" (diikuti jeda panjang) → ✅ IDE SELESAI
```

Jika kamu menemukan klip yang gagal self-check ini, PERBAIKI `end_time`-nya sebelum output. Jangan pernah mengeluarkan klip dengan kalimat putus.

---
### FORMAT OUTPUT (JSON Array):

Kembalikan HANYA JSON array. Tidak boleh ada teks lain sebelum atau sesudahnya.
Setiap item dalam array:
- `"title"`: Judul pendek catchy (Bahasa Indonesia, 5–10 kata). Harus bikin penasaran.
- `"start_time"`: Waktu mulai dalam TOTAL DETIK (INTEGER).
- `"end_time"`: Waktu selesai dalam TOTAL DETIK (INTEGER). PASTIKAN kalimat SELESAI di sini.
- `"virality_score"`: Skor 1–100 (INTEGER), mengikuti rubrik di atas.
- `"reasoning"`: Satu kalimat Bahasa Indonesia — sertakan kategori momen (gunakan SALAH SATU dari: {selected_category_labels}) dan kenapa ini viral.

Jika TIDAK ADA klip yang cocok ke kategori yang dipilih + kalimat selesai + skor ≥ 80, kembalikan: []"""


def _build_prompt(
    user_tier: str,
    video_duration: float,
    transcript_text: str,
    selected_categories: list[str] | None = None,
) -> str:
    """Compose the final prompt with only the user-selected category definitions
    inlined. Saves substantial tokens on every Claude/DeepSeek call."""
    tier_cfg = TIER_DURATIONS.get(user_tier, TIER_DURATIONS["free"])
    min_dur = tier_cfg["min"]
    max_dur = tier_cfg["max"]
    dur_str = f"{video_duration:.1f}" if video_duration else "unknown"

    # Default to all categories for back-compat with stale Celery payloads
    if not selected_categories:
        selected_categories = ALL_CATEGORIES

    # Filter to known categories, preserve user order
    selected = [c for c in selected_categories if c in CATEGORY_DEFINITIONS]
    if not selected:
        selected = ALL_CATEGORIES

    label_to_emoji = {
        "humor": "Lucu",
        "shock": "Kaget",
        "controversy": "Kontroversi",
        "emotional": "Haru",
        "insight": "Insight",
        "motivation": "Motivasi",
    }
    selected_labels = ", ".join(label_to_emoji.get(c, c.title()) for c in selected)
    definitions_block = "\n\n".join(CATEGORY_DEFINITIONS[c] for c in selected)

    return (
        _PROMPT_TEMPLATE
        .replace("{min_duration}", str(min_dur))
        .replace("{max_duration}", str(max_dur))
        .replace("{max_clips}", str(MAX_CLIPS))
        .replace("{video_duration}", dur_str)
        .replace("{selected_category_labels}", selected_labels)
        .replace("{category_definitions}", definitions_block)
        .replace("{transcript_text}", transcript_text)
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
# AI Provider Functions (Dynamic Switch via settings.ai_provider)
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, user_tier: str = "free") -> list[dict] | None:
    """Call Anthropic Claude API with structured JSON output and retry logic."""
    client = Anthropic(api_key=settings.claude_api_key)

    if settings.claude_model:
        model_name = settings.claude_model
    else:
        model_name = "claude-3-5-sonnet-latest"
        
    for attempt in range(3):
        try:
            logger.info(f"Trying Claude {model_name} (attempt {attempt+1}/3)")
            # Build request kwargs — some models (e.g. opus) don't support temperature
            create_kwargs = {
                "model": model_name,
                "max_tokens": 8192,
                "system": "You are an expert content strategist. Return your output exclusively as a valid JSON array matching the requested schema. Do not include any other text.",
                "messages": [{"role": "user", "content": prompt}],
            }
            # Only add temperature for models that support it (not opus/thinking models)
            if "opus" not in model_name.lower():
                create_kwargs["temperature"] = 0.7
            response = client.messages.create(**create_kwargs)

            raw_text = response.content[0].text
            raw_clips = _clean_and_parse_json(raw_text)

            if raw_clips:
                logger.info(f"Claude {model_name} returned {len(raw_clips)} raw clips")
                return raw_clips
                
            logger.warning(f"Claude JSON parse failed, raw: {raw_text[:300]}")

        except Exception as e:
            err_str = str(e).lower()
            if ("429" in err_str or "503" in err_str or "overloaded" in err_str
                    or "rate limit" in err_str or "timed out" in err_str) and attempt < 2:
                wait = 2 ** attempt * 5
                logger.warning(
                    f"Claude {model_name} transient error (attempt {attempt+1}/3), "
                    f"retrying in {wait}s: {e}"
                )
                time.sleep(wait)
            else:
                logger.warning(f"Claude {model_name} failed: {e}")
                break

    return None


def _call_deepseek(prompt: str, user_tier: str = "free") -> list[dict] | None:
    """Call DeepSeek V4 Pro via OpenAI-compatible API with retry logic.

    DeepSeek lacks Gemini's native response_schema, so we rely on
    ``response_format={"type": "json_object"}`` + ``_clean_and_parse_json()``
    for structured output.
    """
    client = OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=180.0,
    )

    model = settings.deepseek_model  # e.g. "deepseek-chat", "deepseek-v4-pro"

    for attempt in range(3):
        try:
            logger.info(f"Calling DeepSeek {model} (attempt {attempt+1}/3)")
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=8192,
            )

            raw_text = response.choices[0].message.content
            finish = response.choices[0].finish_reason
            if not raw_text:
                logger.warning(
                    f"DeepSeek returned empty response (finish_reason={finish}). "
                    f"Check DEEPSEEK_MODEL — '{model}' may not be a valid model ID."
                )
                # Empty response is a config issue, not transient — don't retry
                return None

            clips = _clean_and_parse_json(raw_text)
            if clips:
                logger.info(f"DeepSeek returned {len(clips)} raw clips")
                return clips

            logger.warning(f"DeepSeek JSON parse failed, raw: {raw_text[:300]}")

        except Exception as e:
            err_str = str(e).lower()
            if ("429" in err_str or "503" in err_str or "timed out" in err_str
                    or "rate" in err_str) and attempt < 2:
                wait = 2 ** attempt * 5
                logger.warning(
                    f"DeepSeek transient error (attempt {attempt+1}/3), "
                    f"retrying in {wait}s: {e}"
                )
                time.sleep(wait)
            else:
                logger.error(f"DeepSeek call failed: {e}")
                break

    return None


def _call_ai(prompt: str, user_tier: str = "free") -> list[dict] | None:
    """Dispatch to the configured AI provider."""
    provider = settings.ai_provider
    logger.info(f"AI provider: {provider}")

    if provider == "deepseek":
        return _call_deepseek(prompt, user_tier)
    elif provider == "claude":
        return _call_claude(prompt, user_tier)
    else:
        logger.error(f"Unknown AI_PROVIDER '{provider}' — falling back to DeepSeek")
        return _call_deepseek(prompt, user_tier)


# ---------------------------------------------------------------------------
# Main Extraction Function
# ---------------------------------------------------------------------------

def select_viral_clips(
    transcript_text: str,
    video_duration: float = 0,
    user_tier: str = "free",
    selected_categories: list[str] | None = None,
) -> list[dict]:
    """
    Backward-compatible wrapper for process_video_task.
    Calls extract_and_filter_clips and converts to plain dicts.
    """
    clips = extract_and_filter_clips(
        transcript_text,
        video_duration=video_duration,
        user_tier=user_tier,
        selected_categories=selected_categories,
    )
    return [c.model_dump() for c in clips]


def extract_and_filter_clips(
    transcript_text: str,
    video_duration: float = 0,
    user_tier: str = "free",
    selected_categories: list[str] | None = None,
) -> list[ViralClip]:
    """
    Full pipeline: prompt AI (Claude or DeepSeek) → parse → validate → smart-pad → filter → cap.

    Provider is controlled by the ``AI_PROVIDER`` env var (``"claude"`` or ``"deepseek"``).
    ``selected_categories`` (when provided) restricts the system prompt to only
    those category definitions, saving Claude tokens.

    Returns:
        List of ViralClip objects (score >= 80, padded to min_dur, max 15 items).
    """
    tier_cfg = TIER_DURATIONS.get(user_tier, TIER_DURATIONS["free"])
    min_dur = tier_cfg["min"]
    max_dur = tier_cfg["max"]

    # --- Build prompt ---
    prompt = _build_prompt(user_tier, video_duration, transcript_text, selected_categories)

    # --- Call AI (Claude or DeepSeek based on AI_PROVIDER env var) ---
    cat_log = ",".join(selected_categories) if selected_categories else "ALL"
    logger.info(
        f"Sending transcript to AI provider '{settings.ai_provider}' "
        f"(tier={user_tier}, duration={min_dur}-{max_dur}s, categories={cat_log})"
    )
    raw_clips = _call_ai(prompt, user_tier)

    if not raw_clips:
        logger.warning("AI provider returned no usable clips — returning empty list.")
        return []

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


# ---------------------------------------------------------------------------
# Sentence-Boundary Snapping (post-AI fix for broken endings)
# ---------------------------------------------------------------------------

# Characters that indicate a natural sentence/thought boundary
_SENTENCE_ENDERS = {".", "!", "?", "。", "…"}
# Words that typically signal a thought is complete (Indonesian + English)
_BOUNDARY_WORDS = {
    "gitu", "sih", "ya", "kan", "loh", "nih", "dong", "deh",
    "right", "yeah", "so", "okay", "ok",
}

# Maximum seconds we're willing to extend a clip to find a sentence boundary.
# Beyond this, the AI's pick was just bad — we leave it as-is.
_MAX_EXTENSION_SEC = 8.0


def snap_clip_to_sentence_boundary(
    clip_start: float,
    clip_end: float,
    word_timestamps: list[dict],
    max_duration: float = 60.0,
    video_duration: float = 0.0,
) -> float:
    """Extend ``clip_end`` forward to the nearest sentence boundary.

    Looks at the words immediately after the AI's proposed ``end_time`` and
    finds the first natural stopping point (punctuation, long pause, or
    boundary word). Returns the adjusted ``end_time``.

    If no good boundary is found within ``_MAX_EXTENSION_SEC``, returns the
    original ``clip_end`` unchanged.

    Args:
        clip_start: Start time of the clip (seconds).
        clip_end: AI-proposed end time (seconds).
        word_timestamps: Full transcript word list with ``start``/``end`` keys.
        max_duration: Hard cap on clip duration (tier-based).
        video_duration: Total video length (for clamping).

    Returns:
        Adjusted end_time (>= original clip_end).
    """
    if not word_timestamps:
        return clip_end

    # Find the word closest to clip_end (the last word the AI intended to include)
    end_idx = -1
    for i, w in enumerate(word_timestamps):
        if w["end"] <= clip_end + 0.5:  # 0.5s tolerance for rounding
            end_idx = i
        elif w["start"] > clip_end + 1.0:
            break

    if end_idx < 0:
        return clip_end

    # Check if the clip already ends at a sentence boundary
    last_word = word_timestamps[end_idx]["word"].strip()
    if any(last_word.endswith(p) for p in _SENTENCE_ENDERS):
        return clip_end  # Already good
    if last_word.lower().rstrip(".,!?") in _BOUNDARY_WORDS:
        return clip_end  # Ends on a filler that signals completion

    # Scan forward from end_idx to find the next sentence boundary
    hard_limit = min(
        clip_end + _MAX_EXTENSION_SEC,
        clip_start + max_duration,
        video_duration if video_duration > 0 else float("inf"),
    )

    for i in range(end_idx + 1, len(word_timestamps)):
        w = word_timestamps[i]
        if w["end"] > hard_limit:
            break

        word_text = w["word"].strip()

        # Check for sentence-ending punctuation
        if any(word_text.endswith(p) for p in _SENTENCE_ENDERS):
            logger.debug(
                f"Snapped end_time {clip_end:.1f}s → {w['end']:.1f}s "
                f"(found '{word_text}' at boundary)"
            )
            return w["end"]

        # Check for a long pause after this word (>0.5s gap to next word)
        if i + 1 < len(word_timestamps):
            gap = word_timestamps[i + 1]["start"] - w["end"]
            if gap > 0.5:
                logger.debug(
                    f"Snapped end_time {clip_end:.1f}s → {w['end']:.1f}s "
                    f"(found {gap:.2f}s pause after '{word_text}')"
                )
                return w["end"]

        # Check for boundary words
        if word_text.lower().rstrip(".,!?") in _BOUNDARY_WORDS:
            logger.debug(
                f"Snapped end_time {clip_end:.1f}s → {w['end']:.1f}s "
                f"(boundary word '{word_text}')"
            )
            return w["end"]

    # No good boundary found within limits — return original
    return clip_end