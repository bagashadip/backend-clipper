import logging
from app.tasks.celery_app import celery_app
from app.database import SessionLocal
from app.models import SourceVideo, Clip, Subtitle, User
from app.services.youtube import download_audio, extract_metadata
from app.services.transcription import transcribe_audio
from app.services.ai_selector import select_viral_clips, format_whisper_to_text
from app.services.video_renderer import render_clip, generate_preview_from_clip, generate_thumbnail
from app.services.storage import upload_to_s3

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def process_video_task(self, video_id: str):
    db = SessionLocal()
    audio_path = None

    try:
        video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            logger.error(f"Video {video_id} not found")
            return

        # Step 1: Download audio
        video.status = "downloading"
        db.commit()

        metadata = extract_metadata(video.youtube_url)
        video.title = metadata.get("title")
        video.duration_seconds = metadata.get("duration")
        db.commit()

        audio_path = download_audio(video.youtube_url, video.youtube_id)

        # Step 2: Transcription
        video.status = "transcribing"
        db.commit()

        transcript = transcribe_audio(audio_path)

        # Step 3: AI clip selection
        video.status = "analyzing"
        db.commit()

        formatted_transcript = format_whisper_to_text(transcript["words"])

        # Resolve user tier for duration constraints
        user = db.query(User).filter(User.id == video.user_id).first()
        user_tier = user.subscription_tier if user else "free"

        selected_clips = select_viral_clips(
            formatted_transcript,
            video_duration=video.duration_seconds or 0,
            user_tier=user_tier,
        )

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

            # Render each clip
            render_clip_task.delay(str(clip.id))

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
            import os
            try:
                os.remove(audio_path)
                logger.info(f"Cleaned up audio file: {audio_path}")
            except OSError:
                pass
        db.close()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def render_clip_task(self, clip_id: str):
    import os
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

        # Render the clip
        output_path = render_clip(
            youtube_url=video.youtube_url,
            start_time=clip.start_time,
            end_time=clip.end_time,
            word_timestamps=word_timestamps,
            caption_style=clip.caption_style,
            quality=clip.quality or "480p",
            export_format=clip.export_format or "9:16",
            crop_x_percent=clip.crop_x_percent if clip.crop_x_percent is not None else 50.0,
        )

        # Upload to S3
        storage_url = upload_to_s3(output_path, f"clips/{clip.id}.mp4")
        clip.storage_url = storage_url
        clip.status = "completed"
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
