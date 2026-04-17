import logging
import whisper

logger = logging.getLogger(__name__)

# HAPUS BARIS INI DARI GLOBAL
# model = whisper.load_model("base")

# Bikin variabel penampung kosong
_model_instance = None 

def get_whisper_model():
    """Fungsi ini cuma nge-load model sekali aja ke RAM per Worker"""
    global _model_instance
    if _model_instance is None:
        logger.info("Membangunkan AI Whisper ke memori Worker...")
        _model_instance = whisper.load_model("base")
    return _model_instance

def transcribe_audio(audio_path: str) -> dict:
    logger.info(f"Transcribing audio via openai-whisper: {audio_path}")

    # Panggil AI-nya di dalam fungsi (setelah Celery membelah diri)
    model = get_whisper_model()

    result = model.transcribe(
        audio_path, 
        word_timestamps=True, 
        fp16=False,
        verbose=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        beam_size=1,     
        temperature=0.0  
    )

    words = []
    for segment in result.get("segments", []):
        for w in segment.get("words", []):
            word = w.get("word", "").strip()
            if not word:
                continue
            words.append({
                "word": word,
                "start": float(round(w["start"], 3)),
                "end": float(round(w["end"], 3)),
            })

    transcript = {
        "text": result.get("text", "").strip(),
        "words": words,
    }

    logger.info(f"Transcription complete: {len(words)} words")
    return transcript
