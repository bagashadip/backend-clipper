# Quick Start: YouTube Video Caching

## Setup (5 minutes)

### 1. Run Database Migration
```bash
cd backend
psql -d clipper -U postgres -f migrations/add_source_video_storage_url.sql
```

### 2. Configure S3 Lifecycle (AWS Console)
1. Go to AWS S3 Console
2. Select your bucket
3. Go to **Management** tab → **Lifecycle rules**
4. Click **Create lifecycle rule**
5. Configure:
   - **Rule name**: `Delete source videos after 7 days`
   - **Prefix**: `source_videos/`
   - **Expiration**: `7 days`
   - **Status**: `Enabled`
6. Save

### 3. Verify Environment Variables
Check your `.env` file has:
```bash
AWS_ACCESS_KEY_ID=your_key
AWS_SECRET_ACCESS_KEY=your_secret
AWS_S3_BUCKET=your_bucket
AWS_S3_REGION=us-east-1
```

### 4. Restart Services
```bash
# Restart Celery workers to load new code
docker-compose restart worker

# Or if running locally
celery -A app.tasks.celery_app worker --loglevel=info
```

## How It Works

### First Request (Cache Miss)
```
User submits YouTube URL
    ↓
Extract video ID: dQw4w9WgXcQ
    ↓
Check database: Not found
    ↓
Download full video from YouTube
    ↓
Upload to S3: source_videos/dQw4w9WgXcQ.mp4
    ↓
Save S3 URL in database
    ↓
Render clips from cached source
```

### Second Request (Cache Hit)
```
User submits same YouTube URL
    ↓
Extract video ID: dQw4w9WgXcQ
    ↓
Check database: Found!
    ↓
Verify S3 file exists: Yes
    ↓
Stream from S3 (no YouTube download!)
    ↓
Render clips 60-70% faster
```

## Testing

### Test 1: Cache Miss
```bash
# Submit a new video
curl -X POST http://localhost:8000/api/process-url \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"youtube_url": "https://youtube.com/watch?v=dQw4w9WgXcQ"}'

# Watch logs
tail -f celery_log.txt | grep Cache
# Expected: [Cache MISS] Need to download source video
#           [Cache] Successfully cached source video to S3
```

### Test 2: Cache Hit
```bash
# Submit same video again (or re-render a clip)
curl -X POST http://localhost:8000/api/process-url \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"youtube_url": "https://youtube.com/watch?v=dQw4w9WgXcQ"}'

# Watch logs
tail -f celery_log.txt | grep Cache
# Expected: [Cache HIT] Using cached source video
#           [Cache] Rendering clip using cached source
```

### Test 3: Verify S3
```bash
# List cached videos
aws s3 ls s3://your-bucket/source_videos/

# Check specific video
aws s3 ls s3://your-bucket/source_videos/dQw4w9WgXcQ.mp4

# Get file size
aws s3 ls s3://your-bucket/source_videos/ --human-readable
```

## Monitoring

### Check Cache Performance
```bash
# Count cache hits
grep "Cache HIT" celery_log.txt | wc -l

# Count cache misses
grep "Cache MISS" celery_log.txt | wc -l

# Calculate hit rate
echo "scale=2; $(grep 'Cache HIT' celery_log.txt | wc -l) * 100 / $(grep 'Cache' celery_log.txt | wc -l)" | bc
```

### Check S3 Storage
```bash
# Total storage used
aws s3 ls s3://your-bucket/source_videos/ --recursive --summarize | grep "Total Size"

# Number of cached videos
aws s3 ls s3://your-bucket/source_videos/ | wc -l
```

### Database Queries
```sql
-- Count cached videos
SELECT COUNT(*) FROM source_videos WHERE storage_url IS NOT NULL;

-- List cached videos
SELECT youtube_id, title, storage_url, created_at 
FROM source_videos 
WHERE storage_url IS NOT NULL 
ORDER BY created_at DESC 
LIMIT 10;

-- Find videos with expired cache
SELECT youtube_id, title 
FROM source_videos 
WHERE storage_url IS NOT NULL 
  AND status = 'completed'
  AND created_at < NOW() - INTERVAL '7 days';
```

## Troubleshooting

### Cache Not Working?

**Check 1: Database**
```sql
SELECT youtube_id, storage_url, status FROM source_videos LIMIT 5;
```
- If `storage_url` is NULL → Cache upload failed
- If `status` is not 'completed' → Video processing failed

**Check 2: S3 Access**
```bash
aws s3 ls s3://your-bucket/source_videos/
```
- If "Access Denied" → Check AWS credentials
- If empty → Cache uploads not working

**Check 3: Logs**
```bash
grep "Cache.*failed" celery_log.txt
grep "Error.*cache" celery_log.txt
```

### Common Issues

**Issue**: "Cache MISS" every time
- **Cause**: Database not updated with S3 URL
- **Fix**: Check upload_to_s3() function logs

**Issue**: "Cache EXPIRED" frequently
- **Cause**: S3 lifecycle deleting files too soon
- **Fix**: Increase lifecycle to 14 or 30 days

**Issue**: "S3 object not found"
- **Cause**: Lifecycle deleted file or upload failed
- **Fix**: Check S3 lifecycle rules and upload logs

**Issue**: Slow rendering despite cache hit
- **Cause**: Network streaming slower than expected
- **Fix**: Check S3 region matches worker region

## Performance Metrics

### Expected Results

| Metric | Before Caching | After Caching (Hit) | Improvement |
|--------|---------------|---------------------|-------------|
| Download Time | 30-120s | 0s | 100% |
| Total Render Time | 50-180s | 20-60s | 60-70% |
| Bandwidth Used | 100-500 MB | 0 MB | 100% |
| YouTube API Calls | 1 per render | 0 per render | 100% |

### Cache Hit Rate Targets

- **Week 1**: 20-40% (building cache)
- **Month 1**: 50-70% (stable)
- **Month 3**: 70-85% (mature)

## Best Practices

1. **Monitor cache hit rate** weekly
2. **Adjust lifecycle** based on usage patterns
3. **Clean up failed uploads** periodically
4. **Set up CloudWatch alarms** for S3 storage
5. **Log cache operations** for debugging

## Next Steps

1. ✅ Run migration
2. ✅ Configure S3 lifecycle
3. ✅ Test with sample video
4. ✅ Monitor logs for cache hits
5. ✅ Track performance improvements

## Support

- **Full Documentation**: See `CACHING_SYSTEM.md`
- **Implementation Details**: See `IMPLEMENTATION_SUMMARY.md`
- **Database Schema**: See `schema.sql`
- **Migration Script**: See `migrations/add_source_video_storage_url.sql`
