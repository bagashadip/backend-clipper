import os
import logging
import subprocess
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


def _convert_to_flac(input_path: str) -> str | None:
    """Convert audio file to 16kHz mono FLAC for optimal Groq Whisper compatibility.

    Returns the path to the FLAC file, or None if conversion fails.
    The caller is responsible for cleaning up the returned file.
    """
    output_dir = os.path.dirname(input_path)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    flac_path = os.path.join(output_dir, f"{base_name}.flac")

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-ar", "16000", "-ac", "1",
                "-c:a", "flac",
                flac_path,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.warning(f"FLAC conversion failed: {result.stderr[-300:]}")
            return None

        flac_size_mb = os.path.getsize(flac_path) / (1024 * 1024)
        logger.info(f"Converted to FLAC: {flac_path} ({flac_size_mb:.2f}MB)")
        return flac_path
    except Exception as e:
        logger.warning(f"FLAC conversion error: {e}")
        return None


def _extract_words_from_segments(segments, offset_seconds: float) -> List[Dict[str, Any]]:
    """Extract word-level timestamps from segments as a fallback.

    Some Groq responses put words inside segments rather than at the top level.
    This function also synthesizes approximate word timestamps from segment text
    when per-word data is not available inside segments.
    """
    words: List[Dict[str, Any]] = []

    if not segments:
        return words

    for segment in segments:
        # Try to get words nested inside the segment
        seg_words = None
        if isinstance(segment, dict):
            seg_words = segment.get("words")
        elif hasattr(segment, "words"):
            seg_words = getattr(segment, "words", None)

        if seg_words:
            for w in seg_words:
                if isinstance(w, dict):
                    word_text = w.get("word", "").strip()
                    start = w.get("start", 0.0) + offset_seconds
                    end = w.get("end", 0.0) + offset_seconds
                else:
                    word_text = getattr(w, "word", "").strip()
                    start = getattr(w, "start", 0.0) + offset_seconds
                    end = getattr(w, "end", 0.0) + offset_seconds

                if word_text:
                    words.append({
                        "word": word_text,
                        "start": round(start, 3),
                        "end": round(end, 3),
                    })
        else:
            # Synthesize approximate word timestamps from segment text and timing
            if isinstance(segment, dict):
                seg_text = segment.get("text", "").strip()
                seg_start = segment.get("start", 0.0)
                seg_end = segment.get("end", 0.0)
            else:
                seg_text = getattr(segment, "text", "").strip()
                seg_start = getattr(segment, "start", 0.0)
                seg_end = getattr(segment, "end", 0.0)

            if not seg_text:
                continue

            seg_words_list = seg_text.split()
            if not seg_words_list:
                continue

            duration = seg_end - seg_start
            word_duration = duration / len(seg_words_list) if len(seg_words_list) > 0 else 0

            for i, word_text in enumerate(seg_words_list):
                word_text = word_text.strip()
                if not word_text:
                    continue
                w_start = seg_start + (i * word_duration) + offset_seconds
                w_end = seg_start + ((i + 1) * word_duration) + offset_seconds
                words.append({
                    "word": word_text,
                    "start": round(w_start, 3),
                    "end": round(w_end, 3),
                })

    return words


def _transcribe_chunk(client: Groq, chunk_path: str, offset_seconds: float) -> Dict[str, Any]:
    """Transcribe a single audio chunk and return words with offset applied."""
    logger.info(f"Sending chunk to Groq: {chunk_path}")

    # Determine content type based on file extension
    ext = os.path.splitext(chunk_path)[1].lower()
    content_type_map = {
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
    }
    content_type = content_type_map.get(ext, "audio/mp4")

    with open(chunk_path, "rb") as file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(chunk_path), file.read(), content_type),
            model="whisper-large-v3",
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"]
        )

    words: List[Dict[str, Any]] = []
    full_text_parts = []

    # Handle response depending on whether it's an object or dict
    if hasattr(transcription, "text"):
        full_text_parts.append(transcription.text or "")
    elif isinstance(transcription, dict):
        full_text_parts.append(transcription.get("text", ""))

    # Primary: extract words from top-level 'words' field
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
    elif isinstance(transcription, dict) and transcription.get("words"):
        for w in transcription["words"]:
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

    # Fallback: extract words from segments if top-level words is empty
    if not words:
        segments = None
        if hasattr(transcription, "segments"):
            segments = transcription.segments
        elif isinstance(transcription, dict):
            segments = transcription.get("segments")

        if segments:
            logger.info("Top-level 'words' empty, extracting from segments as fallback")
            words = _extract_words_from_segments(segments, offset_seconds)

    # Log diagnostic info when words are still empty
    text_content = " ".join(full_text_parts).strip()
    if not words and text_content:
        logger.warning(
            f"Groq returned text but 0 word timestamps. "
            f"Text preview: '{text_content[:200]}'"
        )
    elif not words and not text_content:
        logger.warning("Groq returned empty text AND 0 word timestamps — audio may be silent or corrupted")

    return {
        "text": " ".join(full_text_parts),
        "words": words
    }


def transcribe_audio(audio_path: str) -> dict:
    """Transcribe an audio file using Groq's Whisper API with chunking support.

    Converts audio to 16kHz mono FLAC before sending for optimal Whisper
    compatibility (as recommended by Groq docs).

    Returns a dict compatible with ``format_whisper_to_text()``:
        {"text": "<full transcript>", "words": [{"word": ..., "start": ..., "end": ...}, ...]}

    Raises:
        ValueError: If transcription returns 0 words (audio may be silent/corrupted).
    """
    logger.info(f"Transcribing audio via Groq: {audio_path}")
    client = get_groq_client()

    # Convert to FLAC for better Groq Whisper compatibility
    flac_path = _convert_to_flac(audio_path)
    working_path = flac_path if flac_path else audio_path
    if flac_path:
        logger.info(f"Using FLAC-converted audio for transcription: {flac_path}")
    else:
        logger.warning("FLAC conversion failed, using original audio file")

    try:
        # Check file size (Groq limit is 25MB, we use 24MB to be safe)
        MAX_SIZE_BYTES = 24 * 1024 * 1024
        file_size = os.path.getsize(working_path)

        all_words: List[Dict[str, Any]] = []
        full_text_parts: List[str] = []

        if file_size <= MAX_SIZE_BYTES:
            logger.info(f"File size {file_size / (1024*1024):.2f}MB is within limits, sending directly.")
            result = _transcribe_chunk(client, working_path, 0.0)
            full_text_parts.append(result["text"])
            all_words.extend(result["words"])
        else:
            logger.info(f"File size {file_size / (1024*1024):.2f}MB exceeds limit, chunking...")
            audio = AudioSegment.from_file(working_path)

            # 10 minutes chunk size in milliseconds
            chunk_length_ms = 10 * 60 * 1000

            temp_dir = tempfile.mkdtemp(prefix="groq_chunks_")

            try:
                for i, chunk_start_ms in enumerate(range(0, len(audio), chunk_length_ms)):
                    chunk = audio[chunk_start_ms:chunk_start_ms + chunk_length_ms]
                    chunk_path = os.path.join(temp_dir, f"chunk_{i:03d}.flac")

                    logger.info(f"Exporting chunk {i} ({chunk_start_ms/1000:.1f}s to {(chunk_start_ms + len(chunk))/1000:.1f}s)")
                    chunk.export(chunk_path, format="flac")

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

        # Validate: if we got 0 words, raise so the caller can retry with different audio
        if len(all_words) == 0:
            text_preview = transcript["text"][:200] if transcript["text"] else "(empty)"
            raise ValueError(
                f"Transcription returned 0 word-level timestamps. "
                f"Audio may be silent, corrupted, or in an unsupported format. "
                f"Text preview: {text_preview}"
            )

        return transcript

    finally:
        # Clean up FLAC file if we created one
        if flac_path and os.path.exists(flac_path):
            try:
                os.remove(flac_path)
            except OSError:
                pass

