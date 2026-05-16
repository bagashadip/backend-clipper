import uuid
from sqlalchemy import (
    Column, String, Float, Integer, Text, ForeignKey, Enum as SAEnum,
    CheckConstraint, Index, DateTime
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False)
    subscription_tier = Column(
        SAEnum("free", "pro", "enterprise", name="subscription_tier"),
        nullable=False, default="free"
    )
    quota_seconds_remaining = Column(Integer, nullable=False, default=1800)  # 30 min free trial
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    source_videos = relationship("SourceVideo", back_populates="user", cascade="all, delete-orphan")


class SourceVideo(Base):
    __tablename__ = "source_videos"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    youtube_url = Column(Text, nullable=False)
    youtube_id = Column(String(20), nullable=False)
    title = Column(String(500))
    duration_seconds = Column(Float)
    max_source_height = Column(Integer)
    storage_url = Column(Text)  # S3 URL for cached source video
    # User-selected processing config (set during /process-url, read by celery)
    selected_categories = Column(Text)   # comma-separated list, e.g. "humor,insight"
    target_resolution = Column(Integer)  # 480 / 720 / 1080
    target_caption_style = Column(String(40))  # "classic" / "hormozi" / etc.
    status = Column(
        SAEnum("pending", "downloading", "transcribing", "analyzing", "rendering", "completed", "failed",
               name="video_status"),
        nullable=False, default="pending"
    )
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="source_videos")
    clips = relationship("Clip", back_populates="source_video", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_source_videos_user_id", "user_id"),
        Index("idx_source_videos_status", "status"),
        Index("idx_source_videos_youtube_id", "youtube_id"),  # Index for cache lookups
    )


class Clip(Base):
    __tablename__ = "clips"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id = Column(UUID(as_uuid=True), ForeignKey("source_videos.id", ondelete="CASCADE"), nullable=False)
    storage_url = Column(Text)
    thumbnail_url = Column(Text)
    title = Column(String(500))
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    virality_score = Column(Integer)
    reasoning = Column(Text)
    caption_style = Column(
        SAEnum("classic", "hormozi", "hormozi_dynamic", "impact_pop", "typewriter", "none",
               "kenergy", "midnight", "cleanboss", "staccato", "serenity",
               name="caption_style"),
        nullable=False, default="classic"
    )
    quality = Column(
        SAEnum("480p", "720p", "1080p", name="clip_quality"),
        nullable=False, default="480p"
    )
    export_format = Column(String(10), nullable=False, default="9:16", server_default="9:16")
    crop_x_percent = Column(Float, nullable=False, default=50.0, server_default="50.0")
    status = Column(
        SAEnum("pending", "rendering", "completed", "failed", name="clip_status"),
        nullable=False, default="pending"
    )
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    source_video = relationship("SourceVideo", back_populates="clips")
    subtitles = relationship("Subtitle", back_populates="clip", cascade="all, delete-orphan",
                             order_by="Subtitle.sequence_index")

    __table_args__ = (
        CheckConstraint("virality_score >= 0 AND virality_score <= 100", name="ck_virality_score_range"),
        Index("idx_clips_video_id", "video_id"),
    )


class Subtitle(Base):
    __tablename__ = "subtitles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id = Column(UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False)
    word = Column(String(200), nullable=False)
    start_timestamp = Column(Float, nullable=False)
    end_timestamp = Column(Float, nullable=False)
    sequence_index = Column(Integer, nullable=False)

    clip = relationship("Clip", back_populates="subtitles")

    __table_args__ = (
        Index("idx_subtitles_clip_id", "clip_id"),
        Index("idx_subtitles_sequence", "clip_id", "sequence_index"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    order_id = Column(String(255), unique=True, nullable=False)
    gross_amount = Column(Float, nullable=False)
    status = Column(String(50), nullable=False, default="pending")
    payment_type = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")

    __table_args__ = (
        Index("idx_transactions_order_id", "order_id"),
        Index("idx_transactions_user_id", "user_id"),
    )

