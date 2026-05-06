import logging
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# Model loaded once per Celery worker (lazy singleton)
_model_instance: WhisperModel | None = None


def get_whisper_model() -> WhisperModel:
    """Load the Whisper model once per worker process.

    Uses ``large-v3-turbo`` on CUDA with float16 for maximum throughput
    while maintaining near-SOTA accuracy on Indonesian speech.
    """
    global _model_instance
    if _model_instance is None:
        logger.info(
            "Loading faster-whisper large-v3-turbo on CUDA (float16) into worker memory..."
        )
        _model_instance = WhisperModel(
            "large-v3-turbo",
            device="cuda",
            compute_type="float16",
        )
    return _model_instance


def transcribe_audio(audio_path: str) -> dict:
    """Transcribe an audio file and return word-level timestamps.

    Returns a dict compatible with ``format_whisper_to_text()``:
        {"text": "<full transcript>", "words": [{"word": ..., "start": ..., "end": ...}, ...]}
    """
    logger.info(f"Transcribing audio via faster-whisper: {audio_path}")

    model = get_whisper_model()

    # faster-whisper returns (segments_generator, info)
    segments, info = model.transcribe(
        audio_path,
        word_timestamps=True,
        vad_filter=True,             # skip silence — faster & cleaner output
        vad_parameters=dict(
            min_silence_duration_ms=500,
        ),
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        beam_size=1,
        temperature=0.0,
    )

    logger.info(
        f"Detected language: {info.language} (probability: {info.language_probability:.2f})"
    )

    words: list[dict] = []
    full_text_parts: list[str] = []

    # segments is a generator — iterate to collect all words
    for segment in segments:
        full_text_parts.append(segment.text.strip())
        if segment.words:
            for w in segment.words:
                word_text = w.word.strip()
                if not word_text:
                    continue
                words.append({
                    "word": word_text,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                })

    transcript = {
        "text": " ".join(full_text_parts),
        "words": words,
    }

    logger.info(f"Transcription complete: {len(words)} words, duration={info.duration:.1f}s")
    return transcript

