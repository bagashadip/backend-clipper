-- AI Video Clipper - Database Schema

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TYPE subscription_tier AS ENUM ('free', 'pro', 'enterprise');
CREATE TYPE video_status AS ENUM ('pending', 'downloading', 'transcribing', 'analyzing', 'rendering', 'completed', 'failed');
CREATE TYPE clip_status AS ENUM ('pending', 'rendering', 'completed', 'failed');
CREATE TYPE caption_style AS ENUM ('classic', 'hormozi', 'none');

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    subscription_tier subscription_tier NOT NULL DEFAULT 'free',
    quota_seconds_remaining INTEGER NOT NULL DEFAULT 1800,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE source_videos (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    youtube_url TEXT NOT NULL,
    youtube_id VARCHAR(20) NOT NULL,
    title VARCHAR(500),
    duration_seconds FLOAT,
    status video_status NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE clips (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    video_id UUID NOT NULL REFERENCES source_videos(id) ON DELETE CASCADE,
    storage_url TEXT,
    title VARCHAR(500),
    start_time FLOAT NOT NULL,
    end_time FLOAT NOT NULL,
    virality_score INTEGER CHECK (virality_score >= 0 AND virality_score <= 100),
    reasoning TEXT,
    caption_style caption_style NOT NULL DEFAULT 'classic',
    status clip_status NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE subtitles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    clip_id UUID NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    word VARCHAR(200) NOT NULL,
    start_timestamp FLOAT NOT NULL,
    end_timestamp FLOAT NOT NULL,
    sequence_index INTEGER NOT NULL
);

CREATE INDEX idx_source_videos_user_id ON source_videos(user_id);
CREATE INDEX idx_source_videos_status ON source_videos(status);
CREATE INDEX idx_clips_video_id ON clips(video_id);
CREATE INDEX idx_subtitles_clip_id ON subtitles(clip_id);
CREATE INDEX idx_subtitles_sequence ON subtitles(clip_id, sequence_index);
