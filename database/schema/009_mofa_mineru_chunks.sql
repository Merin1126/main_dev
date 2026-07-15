ALTER TABLE mofa_mineru_runs ADD COLUMN input_sha256 TEXT;
ALTER TABLE mofa_mineru_runs ADD COLUMN chunk_index INTEGER;
ALTER TABLE mofa_mineru_runs ADD COLUMN chunk_count INTEGER;
ALTER TABLE mofa_mineru_runs ADD COLUMN chunk_start INTEGER;
ALTER TABLE mofa_mineru_runs ADD COLUMN chunk_end INTEGER;
ALTER TABLE mofa_mineru_runs ADD COLUMN total_pages INTEGER;

CREATE INDEX IF NOT EXISTS idx_mofa_mineru_runs_chunk
    ON mofa_mineru_runs(native_id, chunk_start, chunk_end);
