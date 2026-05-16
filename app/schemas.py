from pydantic import BaseModel, HttpUrl, field_validator
from typing import Optional
from uuid import UUID
from datetime import datetime
import re


# --- Categories (used by the AI selector for dynamic prompt building) ---

VALID_CATEGORIES = {
    "humor",
    "shock",
    "controversy",
    "emotional",
    "insight",
    "motivation",
}


# --- Request Schemas ---

class ProcessURLRequest(BaseModel):
    """Request body for POST /api/process-url.

    Backwards compatible: ``selected_categories``, ``caption_style`` and
    ``resolution`` are optional. Older payloads that only supply
    ``youtube_url`` will fall back to the defaults below.
    """
    youtube_url: str
    selected_categories: Optional[list[str]] = None
    caption_style: Optional[str] = None  # "classic", "hormozi", ... or "none"
    resolution: Optional[int] = None     # 480, 720, or 1080

    @field_validator("youtube_url")
    @classmethod
    def validate_youtube_url(cls, v: str) -> str:
        pattern = r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]{11}"
        if not re.match(pattern, v):
            raise ValueError("Invalid YouTube URL")
        return v

    @field_validator("selected_categories")
    @classmethod
    def validate_categories(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        normalized = [c.strip().lower() for c in v if c and c.strip()]
        invalid = [c for c in normalized if c not in VALID_CATEGORIES]
        if invalid:
            raise ValueError(
                f"Invalid categories: {invalid}. "
                f"Allowed: {sorted(VALID_CATEGORIES)}"
            )
        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for c in normalized:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        if len(deduped) < 2:
            raise ValueError("Pick at least 2 categories")
        return deduped

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v not in (480, 720, 1080):
            raise ValueError("resolution must be 480, 720, or 1080")
        return v

    @field_validator("caption_style")
    @classmethod
    def validate_caption_style(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {
            "classic", "hormozi", "hormozi_dynamic", "impact_pop",
            "typewriter", "none", "kenergy", "midnight",
            "cleanboss", "staccato", "serenity",
        }
        if v not in allowed:
            raise ValueError(f"Invalid caption_style. Allowed: {sorted(allowed)}")
        return v


class UpdateClipRequest(BaseModel):
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    caption_style: Optional[str] = None
    quality: Optional[str] = None
    export_format: Optional[str] = None
    crop_x_percent: Optional[float] = None


class UpdateSubtitleRequest(BaseModel):
    word: str


class CreateSubtitleRequest(BaseModel):
    clip_id: UUID
    word: str
    start_timestamp: float
    end_timestamp: float


# --- Response Schemas ---

class SubtitleResponse(BaseModel):
    id: UUID
    clip_id: UUID
    word: str
    start_timestamp: float
    end_timestamp: float
    sequence_index: int

    model_config = {"from_attributes": True}


class ClipResponse(BaseModel):
    id: UUID
    video_id: UUID
    storage_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    title: Optional[str] = None
    start_time: float
    end_time: float
    virality_score: Optional[int] = None
    reasoning: Optional[str] = None
    caption_style: str
    quality: str
    export_format: str
    crop_x_percent: float
    status: str
    created_at: datetime
    subtitles: list[SubtitleResponse] = []

    model_config = {"from_attributes": True}


class SourceVideoResponse(BaseModel):
    id: UUID
    user_id: UUID
    youtube_url: str
    youtube_id: str
    title: Optional[str] = None
    duration_seconds: Optional[float] = None
    max_source_height: Optional[int] = None
    storage_url: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    clips: list[ClipResponse] = []

    model_config = {"from_attributes": True}


class ProcessURLResponse(BaseModel):
    video_id: UUID
    status: str
    message: str


class YoutubeMetadataResponse(BaseModel):
    """Response for GET /api/youtube/metadata."""
    max_resolution: int
    title: str
    duration: int  # seconds
    cached: bool
