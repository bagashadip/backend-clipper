# Deployment Checklist: YouTube Video Caching

## Pre-Deployment

### Code Review
- [ ] All files modified and tested locally
- [ ] No syntax errors or import issues
- [ ] Type hints are correct (Python 3.10+)
- [ ] Error handling is comprehensive
- [ ] Logging statements are informative

### Database
- [ ] Migration script tested on dev database
- [ ] `storage_url` column added successfully
- [ ] Index `idx_source_videos_youtube_id` created
- [ ] No data loss or corruption
- [ ] Rollback plan documented

### S3 Configuration
- [ ] Bucket exists and is accessible
- [ ] IAM permissions verified (PutObject, GetObject, HeadObject)
- [ ] Lifecycle policy configured (7 days)
- [ ] Region matches application config
- [ ] Endpoint URL correct (if using custom S3)

### Environment Variables
- [ ] `AWS_ACCESS_KEY_ID` set
- [ ] `AWS_SECRET_ACCESS_KEY` set
- [ ] `AWS_S3_BUCKET` set
- [ ] `AWS_S3_REGION` set
- [ ] `S3_ENDPOINT_URL` set (if needed)

## Deployment Steps

### 1. Backup
```bash
# Backup database
pg_dump clipper > backup_$(date +%Y%m%d_%H%M%S).sql

# Backup current code
git tag pre-caching-$(date +%Y%m%d)
git push --tags
```

### 2. Deploy Code
```bash
# Pull latest code
git pull origin main

# Install dependencies (if any new ones)
pip install -r requirements.txt

# Verify imports
python -c "from app.services.youtube import get_youtube_video_id; print('OK')"
python -c "from app.services.storage import check_s3_object_exists; print('OK')"
```

### 3. Run Migration
```bash
# Connect to production database
psql -d clipper -U postgres

# Run migration
\i migrations/add_source_video_storage_url.sql

# Verify
\d source_videos
# Should show: storage_url | text |

\di idx_source_videos_youtube_id
# Should show index details

# Exit
\q
```

### 4. Configure S3 Lifecycle
**AWS Console Steps:**
1. Navigate to S3 → Your Bucket
2. Go to **Management** tab
3. Click **Create lifecycle rule**
4. Configure:
   - Name: `Delete source videos after 7 days`
   - Prefix: `source_videos/`
   - Expiration: `7 days`
   - Status: `Enabled`
5. Review and save

**Verify:**
```bash
aws s3api get-bucket-lifecycle-configuration --bucket your-bucket
```

### 5. Restart Services
```bash
# Docker Compose
docker-compose restart worker

# Or systemd
sudo systemctl restart celery-worker

# Verify workers are running
celery -A app.tasks.celery_app inspect active
```

### 6. Smoke Test
```bash
# Test with a sample video
curl -X POST https://your-domain.com/api/process-url \
  -H "Authorization: Bearer $PROD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"youtube_url": "https://youtube.com/watch?v=dQw4w9WgXcQ"}'

# Monitor logs
tail -f /var/log/celery/worker.log | grep Cache
```

## Post-Deployment

### Verification (First 24 Hours)

#### Hour 1: Immediate Checks
- [ ] No errors in application logs
- [ ] Celery workers processing tasks
- [ ] First video processed successfully
- [ ] S3 upload successful (check bucket)
- [ ] Database updated with `storage_url`

```bash
# Check logs
tail -100 /var/log/celery/worker.log | grep -i error

# Check S3
aws s3 ls s3://your-bucket/source_videos/

# Check database
psql -d clipper -c "SELECT COUNT(*) FROM source_videos WHERE storage_url IS NOT NULL;"
```

#### Hour 6: Cache Hit Test
- [ ] Submit same video again
- [ ] Verify cache hit in logs
- [ ] Confirm faster processing time
- [ ] No YouTube download occurred

```bash
# Check for cache hits
grep "Cache HIT" /var/log/celery/worker.log

# Calculate hit rate
echo "scale=2; $(grep 'Cache HIT' /var/log/celery/worker.log | wc -l) * 100 / $(grep 'Cache' /var/log/celery/worker.log | wc -l)" | bc
```

#### Day 1: Performance Metrics
- [ ] Cache hit rate > 10%
- [ ] No S3 access errors
- [ ] Average render time decreased
- [ ] No increase in error rate

```sql
-- Cache statistics
SELECT 
    COUNT(*) as total_videos,
    COUNT(storage_url) as cached_videos,
    ROUND(COUNT(storage_url)::numeric / COUNT(*) * 100, 2) as cache_percentage
FROM source_videos
WHERE created_at > NOW() - INTERVAL '24 hours';
```

### Monitoring Setup

#### CloudWatch Alarms (AWS)
```bash
# S3 storage alarm (if exceeds 100 GB)
aws cloudwatch put-metric-alarm \
  --alarm-name s3-source-videos-storage \
  --alarm-description "Alert when source_videos exceeds 100GB" \
  --metric-name BucketSizeBytes \
  --namespace AWS/S3 \
  --statistic Average \
  --period 86400 \
  --threshold 107374182400 \
  --comparison-operator GreaterThanThreshold
```

#### Application Metrics
```python
# Add to monitoring dashboard
- cache_hit_rate: (cache_hits / total_requests) * 100
- cache_miss_rate: (cache_misses / total_requests) * 100
- avg_download_time: Average time for YouTube downloads
- avg_render_time: Average time for clip rendering
- s3_upload_failures: Count of failed S3 uploads
```

#### Log Monitoring
```bash
# Set up log alerts for:
- "Cache.*failed"
- "S3.*error"
- "storage_url.*None"
- "Access Denied"
```

### Week 1 Review

#### Performance Metrics
- [ ] Cache hit rate: 20-40%
- [ ] Average render time reduced by 30-50%
- [ ] No increase in error rate
- [ ] S3 storage < 50 GB

#### Issues Encountered
- [ ] Document any issues
- [ ] Create tickets for improvements
- [ ] Update documentation if needed

#### Optimization Opportunities
- [ ] Identify popular videos for pre-caching
- [ ] Adjust lifecycle policy if needed
- [ ] Consider CDN for faster streaming
- [ ] Evaluate storage costs vs benefits

## Rollback Plan

### If Critical Issues Occur

#### Option 1: Code Rollback (Safest)
```bash
# Revert to previous version
git checkout pre-caching-YYYYMMDD
pip install -r requirements.txt
docker-compose restart worker

# Database column can remain (won't cause issues)
```

#### Option 2: Disable Caching (Keep Code)
```python
# In process_video.py, comment out caching:
# cached_video_url, is_cached = get_or_cache_source_video(...)
cached_video_url = None  # Force cache miss
```

#### Option 3: Full Rollback (Nuclear)
```bash
# Restore database backup
psql -d clipper < backup_YYYYMMDD_HHMMSS.sql

# Revert code
git checkout pre-caching-YYYYMMDD

# Restart services
docker-compose restart
```

## Success Criteria

### Week 1
- ✅ Cache hit rate > 20%
- ✅ No critical errors
- ✅ Render time improved by 30%+
- ✅ S3 costs < $10

### Month 1
- ✅ Cache hit rate > 50%
- ✅ Render time improved by 50%+
- ✅ YouTube API calls reduced by 50%+
- ✅ User satisfaction maintained/improved

### Month 3
- ✅ Cache hit rate > 70%
- ✅ Render time improved by 60%+
- ✅ YouTube API calls reduced by 70%+
- ✅ ROI positive (savings > costs)

## Cost Analysis

### S3 Storage Costs
```
Average video size: 300 MB
Videos cached per day: 50
Storage duration: 7 days
Total storage: 50 * 7 * 300 MB = 105 GB

S3 Standard cost: $0.023/GB/month
Monthly cost: 105 GB * $0.023 = $2.42/month
```

### Bandwidth Savings
```
Cache hit rate: 60%
Videos processed per day: 100
Average video size: 300 MB
Bandwidth saved: 100 * 0.6 * 300 MB = 18 GB/day = 540 GB/month

Bandwidth cost saved: 540 GB * $0.09/GB = $48.60/month
```

### Net Savings
```
Savings: $48.60/month
Cost: $2.42/month
Net benefit: $46.18/month (95% reduction)
```

## Support Contacts

- **Database Issues**: DBA Team
- **S3 Issues**: DevOps Team
- **Application Issues**: Backend Team
- **Monitoring**: SRE Team

## Documentation

- [ ] Update README.md with caching info
- [ ] Update API documentation
- [ ] Update deployment guide
- [ ] Update troubleshooting guide
- [ ] Share with team

## Sign-Off

- [ ] Code reviewed by: _______________
- [ ] Database migration approved by: _______________
- [ ] S3 configuration verified by: _______________
- [ ] Deployment executed by: _______________
- [ ] Post-deployment verified by: _______________

**Deployment Date**: _______________
**Deployed By**: _______________
**Status**: ⬜ Success ⬜ Partial ⬜ Rollback
