# YouTube Video Caching System

## Overview

This system implements intelligent caching of YouTube source videos in S3 to:
- **Prevent IP rate-limiting/blocking** from YouTube
- **Save bandwidth** by avoiding repeated downloads
- **Improve performance** for users requesting clips from the same video
- **Enable network streaming** via FFmpeg for efficient clip rendering

## Architecture

### Cache Storage
- **Location**: S3 bucket under `source_videos/{youtube_id}.mp4`
- **Lifecycle**: 7-day automatic deletion (configured in AWS Console)
- **Format**: MP4 with highest available quality (up to 2160p/4K)

### Cache Key
- **Normalized YouTube ID**: 11-character video ID extracted from any URL format
- **Supports**: Standard, Shorts, Mobile (youtu.be), Embed URLs
- **Query params stripped**: `?t=`, `&feature=`, etc. are removed for consistency

## Database Schema

### SourceVideo Model
```python
class SourceVideo:
    youtube_id: str          # 11-char normalized ID (indexed)
    storage_url: str | None  # S3 URL if cached, None otherwise
    max_source_height: int   # Highest resolution available
    status: str              # Processing status
```

**Indexes**:
- `idx_source_videos_youtube_id` - Fast cache lookups
- `idx_source_videos_status` - Filter by completion status

## Caching Workflow

### 1. URL Normalization
```python
youtube_id = get_youtube_video_id(url)
# Extracts: dQw4w9WgXcQ from any YouTube URL format
```

### 2. Cache Lookup
```python
cached_video = db.query(SourceVideo).filter(
    youtube_id == video_id,
    status == "completed",
    storage_url.isnot(None)
).first()
```

### 3. S3 Verification
```python
if cached_video:
    s3_key = f"source_videos/{youtube_id}.mp4"
    if check_s3_object_exists(s3_key):
        # Cache HIT - use S3 URL
        return cached_video.storage_url
    else:
        # Cache EXPIRED - lifecycle deleted it
        cached_video.storage_url = None
```

### 4. Cache Miss Handling
```python
# Download full video at optimal quality
video_path = download_full_video(youtube_url, youtube_id, quality="2160p")

# Upload to S3 with standardized key
s3_url = upload_to_s3(video_path, f"source_videos/{youtube_id}.mp4")

# Update database
video.storage_url = s3_url
```

## Quality Selection

The system intelligently selects download quality based on source video capabilities:

```python
if max_source_height >= 2160:
    cache_quality = "2160p"  # 4K
elif max_source_height >= 1440:
    cache_quality = "1440p"  # 2K
elif max_source_height >= 1080:
    cache_quality = "1080p"  # Full HD
else:
    cache_quality = "best"   # Best available
```

This ensures:
- **720p vertical clips** can be rendered from 1440p+ sources
- **1080p vertical clips** can be rendered from 2160p sources
- No unnecessary bandwidth for low-quality sources

## FFmpeg Network Streaming

When a cache hit occurs, the S3 URL is passed directly to FFmpeg:

```python
# Cache HIT - stream from S3
video = VideoFileClip("https://s3.amazonaws.com/bucket/source_videos/VIDEO_ID.mp4")
video = video.subclip(start_time, end_time)  # Efficient seeking
```

**Benefits**:
- No local disk download required
- FFmpeg seeks directly to timestamp
- Reduced worker disk usage
- Faster clip rendering

## Error Handling

### Non-Fatal Cache Failures
If caching fails, the system gracefully falls back:

```python
try:
    cached_url = cache_source_video_to_s3(url, video_id)
except Exception as e:
    logger.error(f"Cache failed: {e}")
    cached_url = None  # Continue with YouTube download
```

### S3 Lifecycle Expiration
When the 7-day lifecycle deletes a file:

```python
if not check_s3_object_exists(s3_key):
    logger.warning("Cache expired - re-downloading")
    video.storage_url = None  # Clear stale reference
```

## Usage Examples

### First Request (Cache Miss)
```
User submits: https://youtube.com/watch?v=dQw4w9WgXcQ

1. Extract ID: dQw4w9WgXcQ
2. DB lookup: No cached entry found
3. Download: Full video at 2160p (if available)
4. Upload: s3://bucket/source_videos/dQw4w9WgXcQ.mp4
5. Store: DB updated with S3 URL
6. Render: Clips generated from cached source
```

### Second Request (Cache Hit)
```
Another user submits: https://youtu.be/dQw4w9WgXcQ?t=30

1. Extract ID: dQw4w9WgXcQ (normalized)
2. DB lookup: Found cached entry
3. S3 check: File exists
4. Stream: FFmpeg reads directly from S3
5. Render: Clips generated (no YouTube download!)
```

### Re-render Request (Cache Hit)
```
User changes caption style on existing clip

1. Clip ID lookup: Get source video
2. Check cache: storage_url exists
3. S3 verify: File still available
4. Stream: Re-render from cached S3 source
5. No YouTube API calls made
```

## Monitoring & Logs

### Cache Hit
```
[Cache HIT] Using cached source video for dQw4w9WgXcQ from S3
[Cache] Rendering clip abc-123 using cached source: https://s3...
```

### Cache Miss
```
[Cache MISS] Need to download source video for dQw4w9WgXcQ
[Cache] Downloading full video dQw4w9WgXcQ for caching
[Cache] Successfully cached source video to S3: https://s3...
```

### Cache Expired
```
[Cache EXPIRED] S3 file deleted by lifecycle policy for dQw4w9WgXcQ
[Cache MISS] Rendering clip by downloading from YouTube
```

## S3 Lifecycle Configuration

Configure in AWS Console:

```xml
<LifecycleConfiguration>
  <Rule>
    <ID>Delete source videos after 7 days</ID>
    <Prefix>source_videos/</Prefix>
    <Status>Enabled</Status>
    <Expiration>
      <Days>7</Days>
    </Expiration>
  </Rule>
</LifecycleConfiguration>
```

## Migration

To add caching to existing database:

```bash
# Run migration
psql -d clipper -f migrations/add_source_video_storage_url.sql

# Verify
psql -d clipper -c "\d source_videos"
# Should show: storage_url | text |
```

## Performance Impact

### Before Caching
- Every clip render: YouTube download (100-500 MB)
- Risk: IP rate limiting after ~10 videos
- Time: 30-120s download per video

### After Caching
- First render: YouTube download + S3 upload
- Subsequent renders: S3 stream (0 download time)
- Risk: Eliminated (no repeated YouTube requests)
- Time: 0-5s for S3 streaming

## Best Practices

1. **Always normalize URLs** before cache lookups
2. **Verify S3 existence** before using cached URLs
3. **Handle cache failures gracefully** (non-fatal)
4. **Log cache hits/misses** for monitoring
5. **Use highest quality** for caching (enables all output qualities)

## Troubleshooting

### Cache not working?
```python
# Check database
SELECT youtube_id, storage_url, status FROM source_videos WHERE youtube_id = 'VIDEO_ID';

# Check S3
aws s3 ls s3://bucket/source_videos/VIDEO_ID.mp4

# Check logs
grep "Cache" celery_log.txt
```

### S3 access denied?
- Verify AWS credentials in `.env`
- Check S3 bucket permissions
- Ensure bucket region matches config

### Lifecycle deleting too soon?
- Adjust lifecycle policy in AWS Console
- Consider 14 or 30 days for popular videos
- Monitor cache hit rate
