from pydantic import BaseModel, HttpUrl, field_validator
from typing import Optional
from uuid import UUID
from datetime import datetime
import re

VALID_CATEGORIES = {"edukasi", "lucu", "urgent_panas"}


# --- Request Schemas ---

class ProcessURLRequest(BaseModel):
    youtube_url: str
    categories: list[str] = ["edukasi", "lucu", "urgent_panas"]

    @field_validator("youtube_url")
    @classmethod
    def validate_youtube_url(cls, v: str) -> str:
        pattern = r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]{11}"
        if not re.match(pattern, v):
            raise ValueError("Invalid YouTube URL")
        return v

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one category is required")
        invalid = set(v) - VALID_CATEGORIES
        if invalid:
            raise ValueError(f"Invalid categories: {invalid}. Valid: {VALID_CATEGORIES}")
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
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    clips: list[ClipResponse] = []

    model_config = {"from_attributes": True}


class ProcessURLResponse(BaseModel):
    video_id: UUID
    status: str
    message: str
