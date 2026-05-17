import logging
import os
from app.tasks.celery_app import celery_app
from app.database import SessionLocal
from app.models import SourceVideo, Clip, Subtitle, User
from app.services.youtube import download_audio, extract_metadata, get_youtube_video_id, download_full_video
from app.services.transcription import transcribe_audio
from app.services.ai_selector import select_viral_clips, format_whisper_to_text, snap_clip_to_sentence_boundary
from app.services.video_renderer import render_clip, generate_preview_from_clip, generate_thumbnail
from app.services.storage import upload_to_s3, check_s3_object_exists, get_s3_url, generate_presigned_url, _get_s3_client

logger = logging.getLogger(__name__)


def _probe_s3_video_metadata(s3_key: str, youtube_id: str) -> dict:
    """Recover metadata (duration, max height) from an S3-cached video using
    ``ffprobe``. Used when YouTube is blocking us but we already have the
    full video cached in S3.

    Streams metadata from a presigned S3 URL — ffprobe only fetches the
    ``moov`` atom (a few KB) instead of downloading the whole file. This
    keeps the wizard endpoint snappy (~1 s) and avoids burning bandwidth.

    Returns a dict with ``duration`` (seconds, int) and ``max_source_height``
    (px, int). Returns empty values on failure.
    """
    import subprocess
    import json
    from app.services.storage import generate_presigned_url

    try:
        # 5 minute presigned URL is plenty for ffprobe to read the moov atom.
        signed_url = generate_presigned_url(s3_key, expiration=300)
    except Exception as e:
        logger.warning(f"[probe] presign failed for {s3_key}: {e}")
        return {}

    try:
        # ffprobe over HTTP — uses range requests, only pulls the few KB
        # needed to parse format + streams.
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_format", "-show_streams",
                signed_url,
            ],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            logger.warning(f"[probe] ffprobe failed for {s3_key}: {result.stderr[-300:]}")
            return {}

        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning(f"[probe] ffprobe timed out for {s3_key}")
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"[probe] ffprobe returned bad JSON for {s3_key}: {e}")
        return {}

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        return {}

    duration = float(data.get("format", {}).get("duration", 0)) or float(
        video_stream.get("duration", 0)
    )
    height = int(video_stream.get("height", 0) or 0)

    return {
        "duration": int(duration) if duration > 0 else 0,
        "max_source_height": height if height > 0 else None,
    }


def extract_audio_from_s3_video(s3_key: str, youtube_id: str) -> str:
    """
    Download cached source video from S3 and extract audio using FFmpeg.

    Avoids re-downloading from YouTube when the full video is already in S3.
    Falls back to download_audio() if S3 extraction fails.

    Returns:
        Local path to extracted .m4a audio file.
    """
    import subprocess
    import tempfile

    output_dir = tempfile.mkdtemp(prefix="clipper_audio_")
    video_path = os.path.join(output_dir, f"{youtube_id}.mp4")
    audio_path = os.path.join(output_dir, f"{youtube_id}.m4a")

    try:
        client = _get_s3_client()
        from app.config import get_settings
        settings = get_settings()

        logger.info(f"[S3 Audio] Downloading cached video to extract audio: {s3_key}")
        client.download_file(settings.aws_s3_bucket, s3_key, video_path)

        # Extract audio with FFmpeg
        logger.info(f"[S3 Audio] Extracting audio from cached video")
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-acodec", "aac", "-b:a", "128k",
                audio_path,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed: {result.stderr[-300:]}")

        # Clean up video file (keep only audio)
        os.remove(video_path)

        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        logger.info(f"[S3 Audio] Extracted audio: {audio_path} ({file_size_mb:.1f} MB)")
        return audio_path

    except Exception as e:
        logger.warning(f"[S3 Audio] Failed to extract audio from S3 cache: {e}")
        # Cleanup on failure
        for p in [video_path, audio_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        try:
            if os.path.exists(output_dir):
                os.rmdir(output_dir)
        except OSError:
            pass
        raise


def download_cached_video_from_s3(s3_key: str, youtube_id: str) -> str:
    """
    Download a cached video from S3 to local temp directory.
    
    Args:
        s3_key: S3 object key (e.g., "source_videos/VIDEO_ID.mp4")
        youtube_id: YouTube video ID for filename
        
    Returns:
        Local path to downloaded file
    """
    import tempfile
    import boto3
    from app.config import get_settings
    
    settings = get_settings()
    output_dir = tempfile.mkdtemp(prefix="clipper_cached_")
    output_path = os.path.join(output_dir, f"{youtube_id}.mp4")
    
    logger.info(f"[Cache] Downloading cached video from S3: {s3_key}")
    
    try:
        client = _get_s3_client()
        client.download_file(settings.aws_s3_bucket, s3_key, output_path)
        
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(f"[Cache] Downloaded from S3: {output_path} ({file_size_mb:.1f} MB)")
        return output_path
        
    except Exception as e:
        logger.error(f"[Cache] Failed to download from S3: {e}")
        # Cleanup on failure
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
            if os.path.exists(output_dir):
                os.rmdir(output_dir)
        except OSError:
            pass
        raise


def get_or_cache_source_video(youtube_url: str, youtube_id: str, db) -> tuple[str | None, bool]:
    """
    Check if source video is cached in S3, or download and cache it.
    
    Returns:
        (video_path_or_url, is_cached) tuple where:
        - video_path_or_url: Local path to cached file if available, None if needs download
        - is_cached: True if using cached version, False otherwise
    """
    # Step 1: Check database for ANY video with this youtube_id that has storage_url
    # We don't check status because even failed videos may have successfully cached the source
    cached_video = (
        db.query(SourceVideo)
        .filter(
            SourceVideo.youtube_id == youtube_id,
            SourceVideo.storage_url.isnot(None)
        )
        .order_by(SourceVideo.created_at.desc())  # Get most recent
        .first()
    )
    
    if cached_video and cached_video.storage_url:
        # Step 2: Verify the S3 file still exists (7-day lifecycle may have deleted it)
        s3_key = f"source_videos/{youtube_id}.mp4"
        if check_s3_object_exists(s3_key):
            logger.info(f"[Cache HIT] Source video already in S3 for {youtube_id}")
            return cached_video.storage_url, True
        else:
            logger.warning(f"[Cache EXPIRED] S3 file deleted by lifecycle policy for {youtube_id}")
            # Clear the storage_url since the file no longer exists
            cached_video.storage_url = None
            db.commit()
    
    logger.info(f"[Cache MISS] Need to download source video for {youtube_id}")
    return None, False


def cache_source_video_to_s3(youtube_url: str, youtube_id: str, quality: str = "best") -> str | None:
    """
    Download full video and upload to S3 for caching.
    
    Returns:
        S3 URL if successful, None if caching failed (non-fatal)
    """
    local_video_path = None
    try:
        # Download full video
        logger.info(f"[Cache] Downloading full video {youtube_id} for caching")
        local_video_path = download_full_video(youtube_url, youtube_id, quality=quality)
        
        # Upload to S3 with standardized key
        s3_key = f"source_videos/{youtube_id}.mp4"
        s3_url = upload_to_s3(local_video_path, s3_key)
        
        logger.info(f"[Cache] Successfully cached source video to S3: {s3_url}")
        return s3_url
        
    except Exception as e:
        logger.error(f"[Cache] Failed to cache source video {youtube_id}: {e}")
        # Non-fatal: return None and continue with normal processing
        return None
        
    finally:
        # Cleanup local file
        if local_video_path:
            try:
                os.remove(local_video_path)
                parent_dir = os.path.dirname(local_video_path)
                if parent_dir and os.path.exists(parent_dir):
                    os.rmdir(parent_dir)
                logger.info(f"[Cache] Cleaned up local video file: {local_video_path}")
            except OSError as e:
                logger.warning(f"[Cache] Failed to cleanup {local_video_path}: {e}")



@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def process_video_task(self, video_id: str):
    db = SessionLocal()
    audio_path = None

    try:
        video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            logger.error(f"Video {video_id} not found")
            return

        # Step 0: Extract and normalize YouTube ID
        youtube_id = get_youtube_video_id(video.youtube_url)
        video.youtube_id = youtube_id
        db.commit()

        # Step 1: Check cache and download if needed
        video.status = "downloading"
        db.commit()

        # ── Metadata resolution (cache-first, no YouTube hit when avoidable) ──
        # Try DB cache first: any prior row with this youtube_id that has
        # title/duration/max_source_height already populated. Skip rows whose
        # title is the legacy "Video <id>" placeholder so the real title
        # eventually replaces them.
        placeholder_title = f"Video {youtube_id}"
        cached_meta_row = (
            db.query(SourceVideo)
            .filter(
                SourceVideo.youtube_id == youtube_id,
                SourceVideo.duration_seconds.isnot(None),
                SourceVideo.title.isnot(None),
                SourceVideo.title != placeholder_title,
            )
            .order_by(SourceVideo.created_at.desc())
            .first()
        )

        if cached_meta_row:
            logger.info(
                f"[Metadata cache HIT] Reusing metadata from row "
                f"{cached_meta_row.id} for {youtube_id} (no YouTube hit)"
            )
            if not video.title or video.title == placeholder_title:
                video.title = cached_meta_row.title
            if not video.duration_seconds:
                video.duration_seconds = cached_meta_row.duration_seconds
            if not video.max_source_height:
                video.max_source_height = cached_meta_row.max_source_height
            db.commit()
        else:
            # No cached metadata — fall back to yt-dlp.
            try:
                metadata = extract_metadata(video.youtube_url)
                video.title = metadata.get("title")
                video.duration_seconds = metadata.get("duration")
                video.max_source_height = metadata.get("max_source_height")
                db.commit()
            except Exception as e:
                # If YouTube blocks us BUT we already have the source video in
                # S3 from a previous successful run, we can still process the
                # clip using ffprobe to fill in the missing fields.
                logger.warning(f"YouTube metadata fetch failed: {e}")
                s3_key = f"source_videos/{youtube_id}.mp4"
                if check_s3_object_exists(s3_key):
                    logger.info(
                        f"[Metadata recovery] Falling back to ffprobe on S3-cached video"
                    )
                    try:
                        recovered = _probe_s3_video_metadata(s3_key, youtube_id)
                        if recovered.get("duration"):
                            video.duration_seconds = recovered["duration"]
                        if recovered.get("max_source_height"):
                            video.max_source_height = recovered["max_source_height"]
                        # Try lightweight oEmbed for the title — leave NULL
                        # if that fails too (don't poison the DB with a
                        # placeholder; a future run can fill it in).
                        if not video.title or video.title == f"Video {youtube_id}":
                            from app.api.routes import _fetch_youtube_title
                            t = _fetch_youtube_title(youtube_id)
                            if t:
                                video.title = t
                        db.commit()
                    except Exception as probe_err:
                        logger.error(
                            f"[Metadata recovery] ffprobe fallback also failed: {probe_err}"
                        )
                        raise ValueError(
                            f"Cannot fetch metadata from YouTube and no usable cache: {e}"
                        )
                else:
                    raise ValueError(
                        f"Cannot fetch metadata from YouTube and no S3 cache: {e}"
                    )

        # Check if source video is cached in S3
        cached_video_url, is_cached = get_or_cache_source_video(
            video.youtube_url, 
            youtube_id, 
            db
        )
        
        if is_cached and cached_video_url and not video.storage_url:
            # Propagate the cached URL to this video row so S3 audio
            # extraction picks it up (the URL came from a different row).
            video.storage_url = cached_video_url
            db.commit()
            logger.info(f"[Cache] Propagated S3 URL to current video row: {cached_video_url}")
        
        if not is_cached:
            # Cache miss - download and cache the source video
            # Determine quality based on max_source_height for optimal caching
            if video.max_source_height and video.max_source_height >= 2160:
                cache_quality = "2160p"
            elif video.max_source_height and video.max_source_height >= 1440:
                cache_quality = "1440p"
            elif video.max_source_height and video.max_source_height >= 1080:
                cache_quality = "1080p"
            else:
                cache_quality = "best"
            
            cached_video_url = cache_source_video_to_s3(
                video.youtube_url,
                youtube_id,
                quality=cache_quality
            )
            
            # Update database with cached URL (even if None, we tried)
            if cached_video_url:
                video.storage_url = cached_video_url
                db.commit()
                logger.info(f"[Cache] Stored S3 URL in database: {cached_video_url}")

        # Download/extract audio for transcription.
        # Prefer extracting from S3-cached video to avoid re-hitting YouTube.
        s3_key = f"source_videos/{youtube_id}.mp4"
        if video.storage_url and check_s3_object_exists(s3_key):
            try:
                audio_path = extract_audio_from_s3_video(s3_key, youtube_id)
            except Exception as e:
                logger.warning(
                    f"S3 audio extraction failed ({e}), falling back to YouTube download"
                )
                audio_path = download_audio(video.youtube_url, video.youtube_id)
        else:
            logger.info("No S3 cache available, downloading audio from YouTube")
            audio_path = download_audio(video.youtube_url, video.youtube_id)

        # Step 2: Transcription
        video.status = "transcribing"
        db.commit()

        transcript = None
        try:
            transcript = transcribe_audio(audio_path)
        except ValueError as e:
            # Transcription returned 0 words — audio might be corrupted from S3 extraction.
            # Retry by downloading fresh audio directly from YouTube.
            if "0 word-level timestamps" in str(e) and video.storage_url:
                logger.warning(
                    f"Transcription failed with S3-extracted audio: {e}. "
                    f"Retrying with fresh YouTube audio download..."
                )
                # Clean up the bad audio file
                if audio_path and os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                    except OSError:
                        pass
                    audio_path = None

                # Download fresh audio from YouTube
                audio_path = download_audio(video.youtube_url, youtube_id)
                transcript = transcribe_audio(audio_path)
            else:
                raise

        # Step 3: AI clip selection
        video.status = "analyzing"
        db.commit()

        formatted_transcript = format_whisper_to_text(transcript["words"])

        # Resolve user tier for duration constraints
        user = db.query(User).filter(User.id == video.user_id).first()
        user_tier = user.subscription_tier if user else "free"

        # Resolve per-submission processing config (with safe defaults for
        # back-compat with stale Celery payloads).
        selected_categories: list[str] | None = None
        if video.selected_categories:
            selected_categories = [
                c.strip() for c in video.selected_categories.split(",") if c.strip()
            ]
        target_resolution = int(video.target_resolution or 480)
        target_caption_style = video.target_caption_style or "classic"

        # Map resolution → quality string used by Clip.quality enum
        RES_TO_QUALITY = {480: "480p", 720: "720p", 1080: "1080p"}
        target_quality = RES_TO_QUALITY.get(target_resolution, "480p")

        selected_clips = select_viral_clips(
            formatted_transcript,
            video_duration=video.duration_seconds or 0,
            user_tier=user_tier,
            selected_categories=selected_categories,
        )

        # ── Sentence-boundary snapping ────────────────────────────────────
        # The AI often cuts clips mid-sentence. Use the actual word-level
        # timestamps to extend end_time to the nearest natural pause or
        # punctuation mark (up to 8s extra, capped by tier max duration).
        from app.services.ai_selector import TIER_DURATIONS as _TIER_DURS
        _tier_cfg = _TIER_DURS.get(user_tier, _TIER_DURS["free"])
        _max_dur = _tier_cfg["max"]
        _vid_dur = video.duration_seconds or 0

        for c in selected_clips:
            original_end = c["end_time"]
            snapped_end = snap_clip_to_sentence_boundary(
                clip_start=c["start_time"],
                clip_end=c["end_time"],
                word_timestamps=transcript["words"],
                max_duration=_max_dur + 5,  # allow slight overshoot for sentence completion
                video_duration=_vid_dur,
            )
            if snapped_end > original_end:
                logger.info(
                    f"Sentence-snap '{c['title']}': "
                    f"end {original_end:.1f}s → {snapped_end:.1f}s "
                    f"(+{snapped_end - original_end:.1f}s)"
                )
                c["end_time"] = snapped_end

        # Step 4: Create clips and subtitles in DB
        # Filter out clips with timestamps beyond video duration
        video_duration = video.duration_seconds or 0
        valid_clips = []
        for c in selected_clips:
            if c["start_time"] >= video_duration or c["end_time"] > video_duration + 1:
                logger.warning(
                    f"Skipping clip '{c['title']}' — timestamps "
                    f"({c['start_time']:.1f}-{c['end_time']:.1f}s) exceed "
                    f"video duration ({video_duration:.1f}s)"
                )
                continue
            # Clamp end_time to video duration
            c["end_time"] = min(c["end_time"], video_duration)
            valid_clips.append(c)

        if not valid_clips:
            raise ValueError("No valid clips after timestamp validation")

        video.status = "rendering"
        db.commit()

        for clip_data in valid_clips:
            clip = Clip(
                video_id=video.id,
                title=clip_data["title"],
                start_time=clip_data["start_time"],
                end_time=clip_data["end_time"],
                virality_score=clip_data.get("virality_score"),
                reasoning=clip_data.get("reasoning", ""),
                caption_style=target_caption_style,
                quality=target_quality,
                status="pending",
            )
            db.add(clip)
            db.commit()
            db.refresh(clip)

            # Extract word-level timestamps for this clip's time range
            clip_words = [
                w for w in transcript["words"]
                if w["start"] >= clip_data["start_time"] and w["end"] <= clip_data["end_time"]
            ]

            for idx, word_data in enumerate(clip_words):
                subtitle = Subtitle(
                    clip_id=clip.id,
                    word=word_data["word"],
                    start_timestamp=word_data["start"],
                    end_timestamp=word_data["end"],
                    sequence_index=idx,
                )
                db.add(subtitle)

            db.commit()

            # Render each clip (pass None so each task downloads its own isolated copy)
            render_clip_task.delay(str(clip.id), None)

        # --- DEDUCT QUOTA ON SUCCESS ---
        user = db.query(User).filter(User.id == video.user_id).with_for_update().first()
        if user and video.duration_seconds:
            user.quota_seconds_remaining = max(
                0, user.quota_seconds_remaining - int(video.duration_seconds)
            )
        # --- END QUOTA DEDUCTION ---

        video.status = "completed"
        db.commit()

    except Exception as e:
        logger.exception(f"Error processing video {video_id}: {e}")
        video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if video:
            video.status = "failed"
            video.error_message = str(e)[:500]
            db.commit()
        raise self.retry(exc=e)

    finally:
        # Cleanup audio file
        if audio_path:
            try:
                os.remove(audio_path)
                logger.info(f"Cleaned up audio file: {audio_path}")
            except OSError:
                pass
        db.close()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def render_clip_task(self, clip_id: str, cached_video_url: str | None = None):
    db = SessionLocal()

    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            logger.error(f"Clip {clip_id} not found")
            return

        video = db.query(SourceVideo).filter(SourceVideo.id == clip.video_id).first()

        clip.status = "rendering"
        db.commit()

        # Get subtitles for captions
        subtitles = (
            db.query(Subtitle)
            .filter(Subtitle.clip_id == clip.id)
            .order_by(Subtitle.sequence_index)
            .all()
        )

        word_timestamps = [
            {"word": s.word, "start": s.start_timestamp, "end": s.end_timestamp}
            for s in subtitles
        ]

        # Use cached video URL if available, otherwise fall back to YouTube URL
        if cached_video_url:
            logger.info(f"[Cache] Rendering clip {clip_id} using cached source")
        else:
            # Check if the source video has a cached URL in the database
            if video.storage_url:
                # Verify it still exists in S3
                youtube_id = get_youtube_video_id(video.youtube_url)
                s3_key = f"source_videos/{youtube_id}.mp4"
                if check_s3_object_exists(s3_key):
                    try:
                        # Download from S3 to local temp file
                        cached_video_url = download_cached_video_from_s3(s3_key, youtube_id)
                        logger.info(f"[Cache] Found cached source in DB, downloaded from S3")
                    except Exception as e:
                        logger.warning(f"[Cache] Failed to download cached video: {e}")
                else:
                    logger.warning(f"[Cache] Cached source expired, will download from YouTube")
            
            if not cached_video_url:
                logger.info(f"[Cache MISS] Rendering clip {clip_id} by downloading from YouTube")

        # Render the clip
        output_path, effective_quality = render_clip(
            youtube_url=video.youtube_url,
            start_time=clip.start_time,
            end_time=clip.end_time,
            word_timestamps=word_timestamps,
            caption_style=clip.caption_style,
            quality=clip.quality or "480p",
            export_format=clip.export_format or "9:16",
            crop_x_percent=clip.crop_x_percent if clip.crop_x_percent is not None else 50.0,
            cached_video_path=cached_video_url,
        )

        # Upload to S3
        storage_url = upload_to_s3(output_path, f"clips/{clip.id}.mp4")
        clip.storage_url = storage_url
        clip.status = "completed"
        # Save effective quality (may differ from requested if source lacked pixels)
        clip.quality = effective_quality
        db.commit()

        # Generate lightweight preview for editor playback
        try:
            preview_path = generate_preview_from_clip(output_path)
            upload_to_s3(preview_path, f"clips/{clip.id}_preview.mp4")
            logger.info(f"Uploaded preview for clip {clip.id}")
            try:
                os.remove(preview_path)
                os.rmdir(os.path.dirname(preview_path))
            except OSError:
                pass
        except Exception as e:
            logger.warning(f"Preview generation failed for clip {clip.id}: {e}")

        # Generate thumbnail from rendered clip
        try:
            thumb_path = generate_thumbnail(output_path)
            thumb_url = upload_to_s3(thumb_path, f"clips/{clip.id}_thumb.jpg")
            clip.thumbnail_url = thumb_url
            db.commit()
            logger.info(f"Uploaded thumbnail for clip {clip.id}")
            try:
                os.remove(thumb_path)
                os.rmdir(os.path.dirname(thumb_path))
            except OSError:
                pass
        except Exception as e:
            logger.warning(f"Thumbnail generation failed for clip {clip.id}: {e}")

        # Cleanup rendered file
        try:
            os.remove(output_path)
        except OSError:
            pass

    except Exception as e:
        logger.exception(f"Error rendering clip {clip_id}: {e}")
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if clip:
            clip.status = "failed"
            clip.error_message = str(e)[:500]
            db.commit()
        raise self.retry(exc=e)

    finally:
        db.close()
