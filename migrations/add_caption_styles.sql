-- Add 5 new viral caption styles to the caption_style enum
-- PostgreSQL 12+ supports ALTER TYPE ... ADD VALUE in transactions.

-- The caption_style enum already has: classic, hormozi, hormozi_dynamic, impact_pop, typewriter, none
-- Adding: kenergy, midnight, cleanboss, staccato, serenity

ALTER TYPE caption_style ADD VALUE IF NOT EXISTS 'kenergy';
ALTER TYPE caption_style ADD VALUE IF NOT EXISTS 'midnight';
ALTER TYPE caption_style ADD VALUE IF NOT EXISTS 'cleanboss';
ALTER TYPE caption_style ADD VALUE IF NOT EXISTS 'staccato';
ALTER TYPE caption_style ADD VALUE IF NOT EXISTS 'serenity';
