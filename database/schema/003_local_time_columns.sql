-- 阶段 4.1：增加本地时区（+08:00）字段，便于 GUI / 人工审计直接读取

-- download_runs 本地时间
ALTER TABLE download_runs ADD COLUMN started_at_local TEXT;
ALTER TABLE download_runs ADD COLUMN finished_at_local TEXT;

UPDATE download_runs
SET started_at_local = (
    strftime('%Y-%m-%dT%H:%M:%S', datetime(started_at, '+8 hours')) || '+08:00'
)
WHERE started_at_local IS NULL
  AND started_at IS NOT NULL;

UPDATE download_runs
SET finished_at_local = (
    strftime('%Y-%m-%dT%H:%M:%S', datetime(finished_at, '+8 hours')) || '+08:00'
)
WHERE finished_at_local IS NULL
  AND finished_at IS NOT NULL;

-- download_events 本地时间
ALTER TABLE download_events ADD COLUMN timestamp_local TEXT;

UPDATE download_events
SET timestamp_local = (
    strftime('%Y-%m-%dT%H:%M:%S', datetime(COALESCE(timestamp, recorded_at), '+8 hours')) || '+08:00'
)
WHERE timestamp_local IS NULL
  AND COALESCE(timestamp, recorded_at) IS NOT NULL;
