import re
from uuid import UUID
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import SourceVideo, Clip, Subtitle, User
from app.schemas import (
    ProcessURLRequest, ProcessURLResponse,
    SourceVideoResponse, ClipResponse,
    UpdateClipRequest, UpdateSubtitleRequest, CreateSubtitleRequest,
    YoutubeMetadataResponse, VALID_CATEGORIES,
)
from app.services.youtube import check_video_duration, extract_metadata, get_youtube_video_id
from app.services.auth import get_current_user
from app.services.storage import generate_presigned_url, upload_to_s3, check_s3_object_exists
from app.tasks.process_video import process_video_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["clips"])


def _extract_youtube_id(url: str) -> str:
    patterns = [
        r"(?:v=|youtu\.be/)([\w-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise HTTPException(status_code=400, detail="Could not extract YouTube video ID")


# ── Pre-fetch metadata endpoint ────────────────────────────────────────────

@router.get("/youtube/metadata", response_model=YoutubeMetadataResponse)
def get_youtube_metadata(
    url: str = Query(..., description="Full YouTube URL"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fetch lightweight metadata (title, duration, max resolution) for a
    YouTube video, used by the wizard to gate the resolution step.

    Resolution order:
      1. Check our DB / S3 cache. If the source video is already cached,
         return its stored metadata WITHOUT calling YouTube.
      2. Fall back to yt-dlp with skip_download=True (equivalent to
         ``yt-dlp --dump-json --no-download``) — typically 1–2s.
    """
    # Parse the YouTube ID first so we fail fast on garbage URLs
    try:
        youtube_id = get_youtube_video_id(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # ── Step 1 + 2: cache lookup ─────────────────────────────────────────
    cached = (
        db.query(SourceVideo)
        .filter(
            SourceVideo.youtube_id == youtube_id,
            SourceVideo.storage_url.isnot(None),
            SourceVideo.max_source_height.isnot(None),
            SourceVideo.duration_seconds.isnot(None),
        )
        .order_by(SourceVideo.created_at.desc())
        .first()
    )

    if cached:
        s3_key = f"source_videos/{youtube_id}.mp4"
        if check_s3_object_exists(s3_key):
            return YoutubeMetadataResponse(
                max_resolution=int(cached.max_source_height or 0),
                title=cached.title or "",
                duration=int(cached.duration_seconds or 0),
                cached=True,
            )

    # ── Step 2b: relaxed cache lookup — S3 has the file but DB row may
    # lack height/duration. Fall back to ffprobe so we can still skip
    # YouTube entirely.
    s3_key = f"source_videos/{youtube_id}.mp4"
    has_s3_file = check_s3_object_exists(s3_key)
    title_fallback = ""
    if has_s3_file:
        any_row = (
            db.query(SourceVideo)
            .filter(SourceVideo.youtube_id == youtube_id)
            .order_by(SourceVideo.created_at.desc())
            .first()
        )
        if any_row and any_row.title:
            title_fallback = any_row.title

        try:
            from app.tasks.process_video import _probe_s3_video_metadata
            probed = _probe_s3_video_metadata(s3_key, youtube_id)
            if probed.get("duration"):
                return YoutubeMetadataResponse(
                    max_resolution=int(probed.get("max_source_height") or 0),
                    title=title_fallback or f"Video {youtube_id}",
                    duration=int(probed["duration"]),
                    cached=True,
                )
        except Exception as probe_err:
            logger.warning(f"ffprobe S3 fallback failed for {youtube_id}: {probe_err}")

    # ── Step 3: yt-dlp fallback (--dump-json --no-download equivalent) ───
    try:
        meta = extract_metadata(url)
    except ValueError as e:
        msg = str(e).lower()
        if "sign in" in msg or "bot" in msg or "confirm you" in msg:
            raise HTTPException(
                status_code=503,
                detail=(
                    "YouTube is blocking automated access for this video. "
                    "Try a different video or wait a few minutes."
                ),
            )
        raise HTTPException(status_code=502, detail=f"YouTube metadata fetch failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"YouTube metadata fetch failed: {e}")

    return YoutubeMetadataResponse(
        max_resolution=int(meta.get("max_source_height") or 0),
        title=meta.get("title", ""),
        duration=int(meta.get("duration", 0) or 0),
        cached=False,
    )


@router.post("/process-url", response_model=ProcessURLResponse)
def process_url(
    request: ProcessURLRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    youtube_id = _extract_youtube_id(request.youtube_url)

    # --- QUOTA GATEKEEPER ---
    # Cache-first: avoid hitting YouTube when we already know the duration.
    video_duration: int | None = None
    cached_row = (
        db.query(SourceVideo)
        .filter(
            SourceVideo.youtube_id == youtube_id,
            SourceVideo.duration_seconds.isnot(None),
        )
        .order_by(SourceVideo.created_at.desc())
        .first()
    )
    if cached_row and cached_row.duration_seconds:
        video_duration = int(cached_row.duration_seconds)
        logger.info(
            f"[process-url] Using cached duration {video_duration}s for {youtube_id} "
            f"(no YouTube hit)"
        )
    else:
        try:
            video_duration = check_video_duration(request.youtube_url)
        except ValueError as e:
            # If YouTube blocks us but we already cached the source video, fall
            # back to ffprobe on the S3 file.
            s3_key = f"source_videos/{youtube_id}.mp4"
            if check_s3_object_exists(s3_key):
                try:
                    from app.tasks.process_video import _probe_s3_video_metadata
                    probed = _probe_s3_video_metadata(s3_key, youtube_id)
                    if probed.get("duration"):
                        video_duration = int(probed["duration"])
                        logger.info(
                            f"[process-url] Recovered duration via ffprobe on S3 "
                            f"cache for {youtube_id}: {video_duration}s"
                        )
                except Exception as probe_err:
                    logger.warning(
                        f"[process-url] ffprobe S3 fallback failed: {probe_err}"
                    )
            if not video_duration:
                raise HTTPException(status_code=400, detail=str(e))

    if video_duration > user.quota_seconds_remaining:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient quota. Video is {video_duration}s, "
                f"but you only have {user.quota_seconds_remaining}s remaining. "
                f"Please upgrade your plan."
            ),
        )
    # --- END GATEKEEPER ---

    # --- TIER GATE for resolution ---
    requested_resolution = request.resolution or 480
    if user.subscription_tier == "free" and requested_resolution > 480:
        raise HTTPException(
            status_code=403,
            detail="HD resolution (720p / 1080p) requires a Pro subscription.",
        )

    # Default categories: all 6 if none provided (back-compat with older clients)
    selected = request.selected_categories or sorted(VALID_CATEGORIES)
    caption_style = request.caption_style or "classic"

    video = SourceVideo(
        user_id=user.id,
        youtube_url=request.youtube_url,
        youtube_id=youtube_id,
        duration_seconds=video_duration,
        selected_categories=",".join(selected),
        target_resolution=requested_resolution,
        target_caption_style=caption_style,
        status="pending",
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    process_video_task.delay(str(video.id))

    return ProcessURLResponse(
        video_id=video.id,
        status="pending",
        message="Video processing started",
    )


@router.get("/videos/{video_id}", response_model=SourceVideoResponse)
def get_video(video_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    video = (
        db.query(SourceVideo)
        .options(joinedload(SourceVideo.clips).joinedload(Clip.subtitles))
        .filter(SourceVideo.id == video_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


@router.get("/videos", response_model=list[SourceVideoResponse])
def list_videos(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    videos = (
        db.query(SourceVideo)
        .options(joinedload(SourceVideo.clips).joinedload(Clip.subtitles))
        .filter(SourceVideo.user_id == user.id)
        .order_by(SourceVideo.created_at.desc())
        .all()
    )
    return videos


@router.get("/clips/{clip_id}", response_model=ClipResponse)
def get_clip(clip_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    clip = (
        db.query(Clip)
        .options(joinedload(Clip.subtitles))
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Clip.id == clip_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    return clip


@router.patch("/clips/{clip_id}", response_model=ClipResponse)
def update_clip(
    clip_id: UUID,
    request: UpdateClipRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    clip = (
        db.query(Clip)
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Clip.id == clip_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    if request.start_time is not None:
        clip.start_time = request.start_time
    if request.end_time is not None:
        clip.end_time = request.end_time
    if request.caption_style is not None:
        if request.caption_style not in ("classic", "hormozi", "hormozi_dynamic", "impact_pop", "typewriter", "none", "kenergy", "midnight", "cleanboss", "staccato", "serenity"):
            raise HTTPException(status_code=400, detail="Invalid caption style")
        clip.caption_style = request.caption_style
    if request.quality is not None:
        if request.quality not in ("480p", "720p", "1080p"):
            raise HTTPException(status_code=400, detail="Invalid quality. Choose 480p, 720p, or 1080p")
        if request.quality != "480p" and user.subscription_tier == "free":
            raise HTTPException(status_code=403, detail="HD quality requires a Pro subscription. Upgrade to unlock 720p and 1080p.")
        clip.quality = request.quality
    if request.export_format is not None:
        if request.export_format not in ("9:16", "16:9"):
            raise HTTPException(status_code=400, detail="Invalid format. Choose 9:16 or 16:9")
        if request.export_format == "16:9" and user.subscription_tier == "free":
            raise HTTPException(status_code=403, detail="Landscape export requires a Pro subscription.")
        clip.export_format = request.export_format
    if request.crop_x_percent is not None:
        if not (0 <= request.crop_x_percent <= 100):
            raise HTTPException(status_code=400, detail="crop_x_percent must be between 0 and 100")
        if user.subscription_tier == "free":
            raise HTTPException(status_code=403, detail="Custom crop position requires a Pro subscription.")
        clip.crop_x_percent = request.crop_x_percent

    clip.status = "pending"
    db.commit()
    db.refresh(clip)

    # Re-render with new settings
    from app.tasks.process_video import render_clip_task
    render_clip_task.delay(str(clip.id))

    return db.query(Clip).options(joinedload(Clip.subtitles)).filter(Clip.id == clip_id).first()


@router.post("/clips/{clip_id}/proxy")
def generate_clip_proxy(
    clip_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate a lightweight 16:9 proxy video for crop positioning (Pro only)."""
    if user.subscription_tier == "free":
        raise HTTPException(status_code=403, detail="Proxy editing requires a Pro subscription.")

    clip = (
        db.query(Clip)
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Clip.id == clip_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    video = db.query(SourceVideo).filter(SourceVideo.id == clip.video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Source video not found")

    # Check if proxy already exists in S3
    proxy_key = f"proxies/{clip.id}.mp4"
    try:
        from app.services.storage import _get_s3_client
        from app.config import get_settings
        s3_settings = get_settings()
        s3_client = _get_s3_client()
        s3_client.head_object(Bucket=s3_settings.aws_s3_bucket, Key=proxy_key)
        url = generate_presigned_url(proxy_key, expiration=3600)
        return {"proxy_url": url}
    except Exception:
        pass

    # Generate proxy
    import os
    import logging
    from app.services.video_renderer import generate_proxy_video

    cached_video_url = None
    try:
        if video.storage_url:
            from app.services.youtube import get_youtube_video_id
            from app.services.storage import check_s3_object_exists
            from app.tasks.process_video import download_cached_video_from_s3

            youtube_id = get_youtube_video_id(video.youtube_url)
            s3_key = f"source_videos/{youtube_id}.mp4"
            if check_s3_object_exists(s3_key):
                try:
                    cached_video_url = download_cached_video_from_s3(s3_key, youtube_id)
                except Exception as e:
                    pass

        proxy_path = generate_proxy_video(
            youtube_url=video.youtube_url,
            start_time=clip.start_time,
            end_time=clip.end_time,
            cached_video_path=cached_video_url,
        )
        upload_to_s3(proxy_path, proxy_key)
        url = generate_presigned_url(proxy_key, expiration=3600)

        # Cleanup
        try:
            os.remove(proxy_path)
            os.rmdir(os.path.dirname(proxy_path))
        except OSError:
            pass

        if cached_video_url and os.path.exists(cached_video_url):
            try:
                os.remove(cached_video_url)
                os.rmdir(os.path.dirname(cached_video_url))
            except OSError:
                pass

        return {"proxy_url": url}
    except Exception as e:
        # Cleanup on failure too
        if cached_video_url and os.path.exists(cached_video_url):
            try:
                os.remove(cached_video_url)
                os.rmdir(os.path.dirname(cached_video_url))
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"Failed to generate proxy: {str(e)[:200]}")


@router.patch("/subtitles/{subtitle_id}", response_model=dict)
def update_subtitle(subtitle_id: UUID, request: UpdateSubtitleRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    subtitle = (
        db.query(Subtitle)
        .join(Clip, Subtitle.clip_id == Clip.id)
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Subtitle.id == subtitle_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not subtitle:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    subtitle.word = request.word
    db.commit()
    return {"status": "updated"}


@router.post("/subtitles", response_model=dict)
def create_subtitle(
    request: CreateSubtitleRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a new subtitle word at a specific timestamp."""
    clip = (
        db.query(Clip)
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Clip.id == request.clip_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    # Determine sequence_index: find where this subtitle fits chronologically
    existing = (
        db.query(Subtitle)
        .filter(Subtitle.clip_id == request.clip_id)
        .order_by(Subtitle.sequence_index)
        .all()
    )

    # Find insert position based on start_timestamp
    insert_idx = 0
    for i, s in enumerate(existing):
        if request.start_timestamp >= s.start_timestamp:
            insert_idx = i + 1

    # Shift subsequent subtitles' sequence_index up by 1
    for s in existing[insert_idx:]:
        s.sequence_index += 1

    subtitle = Subtitle(
        clip_id=request.clip_id,
        word=request.word,
        start_timestamp=request.start_timestamp,
        end_timestamp=request.end_timestamp,
        sequence_index=insert_idx,
    )
    db.add(subtitle)
    db.commit()
    db.refresh(subtitle)

    return {"status": "created", "id": str(subtitle.id), "sequence_index": insert_idx}


@router.delete("/subtitles/{subtitle_id}", response_model=dict)
def delete_subtitle(
    subtitle_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a subtitle word."""
    subtitle = (
        db.query(Subtitle)
        .join(Clip, Subtitle.clip_id == Clip.id)
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Subtitle.id == subtitle_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not subtitle:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    clip_id = subtitle.clip_id
    deleted_idx = subtitle.sequence_index
    db.delete(subtitle)

    # Shift subsequent subtitles' sequence_index down by 1
    remaining = (
        db.query(Subtitle)
        .filter(Subtitle.clip_id == clip_id, Subtitle.sequence_index > deleted_idx)
        .all()
    )
    for s in remaining:
        s.sequence_index -= 1

    db.commit()
    return {"status": "deleted"}


@router.get("/clips/{clip_id}/download")
def get_clip_download_url(
    clip_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a presigned S3 URL for downloading/streaming the clip."""
    clip = (
        db.query(Clip)
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Clip.id == clip_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if not clip.storage_url:
        raise HTTPException(status_code=404, detail="Clip not yet rendered")

    s3_key = f"clips/{clip.id}.mp4"
    presigned_url = generate_presigned_url(s3_key, expiration=3600)
    return {"url": presigned_url}


@router.get("/clips/{clip_id}/preview")
def get_clip_preview_url(
    clip_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a presigned S3 URL for the lightweight preview version (editor playback)."""
    clip = (
        db.query(Clip)
        .join(SourceVideo, Clip.video_id == SourceVideo.id)
        .filter(Clip.id == clip_id, SourceVideo.user_id == user.id)
        .first()
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if not clip.storage_url:
        raise HTTPException(status_code=404, detail="Clip not yet rendered")

    # Try preview first, fall back to full quality
    preview_key = f"clips/{clip.id}_preview.mp4"
    try:
        from app.services.storage import _get_s3_client
        from app.config import get_settings
        s3_settings = get_settings()
        s3_client = _get_s3_client()
        s3_client.head_object(Bucket=s3_settings.aws_s3_bucket, Key=preview_key)
        url = generate_presigned_url(preview_key, expiration=3600)
        return {"url": url}
    except Exception:
        # Fallback to full quality if preview doesn't exist yet
        s3_key = f"clips/{clip.id}.mp4"
        presigned_url = generate_presigned_url(s3_key, expiration=3600)
        return {"url": presigned_url}


@router.get("/clips/{clip_id}/thumbnail")
def get_clip_thumbnail(clip_id: UUID, db: Session = Depends(get_db)):
    """Redirect to a presigned S3 URL for the clip thumbnail."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if not clip.thumbnail_url:
        raise HTTPException(status_code=404, detail="No thumbnail available")

    s3_key = f"clips/{clip.id}_thumb.jpg"
    url = generate_presigned_url(s3_key, expiration=3600)
    return RedirectResponse(url=url, status_code=307)


@router.get("/quota")
def get_quota(user: User = Depends(get_current_user)):
    return {
        "quota_seconds_remaining": user.quota_seconds_remaining,
        "subscription_tier": user.subscription_tier,
    }
