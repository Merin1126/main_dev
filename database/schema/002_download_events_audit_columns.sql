-- 为已存在的 download_events 表补充阶段 2.1 审计字段
ALTER TABLE download_events ADD COLUMN ref_code TEXT;
ALTER TABLE download_events ADD COLUMN event_type TEXT;
ALTER TABLE download_events ADD COLUMN message TEXT;
ALTER TABLE download_events ADD COLUMN timestamp TEXT;

-- 将已有历史数据尽可能回填到新字段，便于统一查询
UPDATE download_events
SET ref_code = CASE
        WHEN document_id IS NULL THEN NULL
        WHEN instr(document_id, ':') = 0 THEN document_id
        ELSE substr(document_id, instr(document_id, ':') + 1)
    END
WHERE ref_code IS NULL;

UPDATE download_events
SET event_type = status
WHERE event_type IS NULL;

UPDATE download_events
SET message = error_message
WHERE message IS NULL;

UPDATE download_events
SET timestamp = recorded_at
WHERE timestamp IS NULL;
