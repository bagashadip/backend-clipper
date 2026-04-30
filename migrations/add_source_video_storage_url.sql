-- Migration: Add storage_url column to source_videos table for caching
-- This allows storing S3 URLs for cached source videos

-- Add storage_url column
ALTER TABLE source_videos ADD COLUMN IF NOT EXISTS storage_url TEXT;

-- Add index on youtube_id for efficient cache lookups
CREATE INDEX IF NOT EXISTS idx_source_videos_youtube_id ON source_videos(youtube_id);

-- Add comment explaining the column
COMMENT ON COLUMN source_videos.storage_url IS 'S3 URL for cached source video (7-day lifecycle)';
