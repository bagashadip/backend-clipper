-- Add per-submission processing config to source_videos.
-- These are populated by POST /api/process-url and read by the celery task.

ALTER TABLE source_videos
    ADD COLUMN IF NOT EXISTS selected_categories TEXT,
    ADD COLUMN IF NOT EXISTS target_resolution   INTEGER,
    ADD COLUMN IF NOT EXISTS target_caption_style VARCHAR(40);
