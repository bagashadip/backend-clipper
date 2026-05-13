import os
import logging
import tempfile
from typing import Any, Dict, List
from groq import Groq
from pydub import AudioSegment

logger = logging.getLogger(__name__)

# Model loaded once per Celery worker (lazy singleton)
_groq_client: Groq | None = None


def get_groq_client() -> Groq:
    """Initialize the Groq client once per worker process."""
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is missing.")
        logger.info("Initializing Groq API client...")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def _transcribe_chunk(client: Groq, chunk_path: str, offset_seconds: float) -> Dict[str, Any]:
    """Transcribe a single audio chunk and return words with offset applied."""
    logger.info(f"Sending chunk to Groq: {chunk_path}")
    
    with open(chunk_path, "rb") as file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(chunk_path), file.read()),
            model="whisper-large-v3",
            response_format="verbose_json",
            timestamp_granularities=["word"]
        )

    words: List[Dict[str, Any]] = []
    full_text_parts = []
    
    # Handle response depending on whether it's an object or dict
    if hasattr(transcription, "text"):
        full_text_parts.append(transcription.text)
        
    if hasattr(transcription, "words") and transcription.words:
        for w in transcription.words:
            word_text = getattr(w, "word", "").strip()
            if not word_text:
                continue
            start_time = getattr(w, "start", 0.0) + offset_seconds
            end_time = getattr(w, "end", 0.0) + offset_seconds
            
            words.append({
                "word": word_text,
                "start": round(start_time, 3),
                "end": round(end_time, 3),
            })
    elif isinstance(transcription, dict):
        full_text_parts.append(transcription.get("text", ""))
        for w in transcription.get("words", []):
            word_text = w.get("word", "").strip()
            if not word_text:
                continue
            start_time = w.get("start", 0.0) + offset_seconds
            end_time = w.get("end", 0.0) + offset_seconds
            
            words.append({
                "word": word_text,
                "start": round(start_time, 3),
                "end": round(end_time, 3),
            })
            
    return {
        "text": " ".join(full_text_parts),
        "words": words
    }


def transcribe_audio(audio_path: str) -> dict:
    """Transcribe an audio file using Groq's Whisper API with chunking support.

    Returns a dict compatible with ``format_whisper_to_text()``:
        {"text": "<full transcript>", "words": [{"word": ..., "start": ..., "end": ...}, ...]}
    """
    logger.info(f"Transcribing audio via Groq: {audio_path}")
    client = get_groq_client()
    
    # Check file size (Groq limit is 25MB, we use 24MB to be safe)
    MAX_SIZE_BYTES = 24 * 1024 * 1024
    file_size = os.path.getsize(audio_path)
    
    all_words: List[Dict[str, Any]] = []
    full_text_parts: List[str] = []
    
    if file_size <= MAX_SIZE_BYTES:
        logger.info(f"File size {file_size / (1024*1024):.2f}MB is within limits, sending directly.")
        result = _transcribe_chunk(client, audio_path, 0.0)
        full_text_parts.append(result["text"])
        all_words.extend(result["words"])
    else:
        logger.info(f"File size {file_size / (1024*1024):.2f}MB exceeds limit, chunking...")
        audio = AudioSegment.from_file(audio_path)
        
        # 10 minutes chunk size in milliseconds
        chunk_length_ms = 10 * 60 * 1000
        
        temp_dir = tempfile.mkdtemp(prefix="groq_chunks_")
        
        try:
            for i, chunk_start_ms in enumerate(range(0, len(audio), chunk_length_ms)):
                chunk = audio[chunk_start_ms:chunk_start_ms + chunk_length_ms]
                chunk_path = os.path.join(temp_dir, f"chunk_{i:03d}.m4a")
                
                logger.info(f"Exporting chunk {i} ({chunk_start_ms/1000:.1f}s to {(chunk_start_ms + len(chunk))/1000:.1f}s)")
                chunk.export(chunk_path, format="ipod", codec="aac", parameters=["-b:a", "128k"])
                
                offset_seconds = chunk_start_ms / 1000.0
                result = _transcribe_chunk(client, chunk_path, offset_seconds)
                
                full_text_parts.append(result["text"])
                all_words.extend(result["words"])
                
                try:
                    os.remove(chunk_path)
                except Exception as e:
                    logger.warning(f"Failed to remove temp chunk {chunk_path}: {e}")
        finally:
            try:
                os.rmdir(temp_dir)
            except Exception:
                pass

    transcript = {
        "text": " ".join([t for t in full_text_parts if t.strip()]),
        "words": all_words,
    }

    logger.info(f"Transcription complete: {len(all_words)} words")
    return transcript

