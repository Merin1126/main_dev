CREATE TABLE IF NOT EXISTS mofa_mineru_runs (
    run_id            TEXT PRIMARY KEY,
    document_id       TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    native_id         TEXT NOT NULL,
    result_signature  TEXT NOT NULL,
    source_dir        TEXT NOT NULL,
    raw_dir           TEXT NOT NULL,
    match_method      TEXT NOT NULL,
    mineru_version    TEXT,
    backend           TEXT,
    effort            TEXT,
    ocr_enabled       INTEGER,
    page_count        INTEGER,
    imported_at       TEXT NOT NULL,
    metadata_json     TEXT,
    UNIQUE(document_id, result_signature)
);

CREATE INDEX IF NOT EXISTS idx_mofa_mineru_runs_document
    ON mofa_mineru_runs(document_id, imported_at);

CREATE INDEX IF NOT EXISTS idx_mofa_mineru_runs_signature
    ON mofa_mineru_runs(result_signature);

CREATE TABLE IF NOT EXISTS mofa_mineru_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
