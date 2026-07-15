CREATE TABLE IF NOT EXISTS mofa_ocr_generations (
    generation_id             TEXT PRIMARY KEY,
    document_id               TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    native_id                 TEXT NOT NULL,
    source_signature          TEXT NOT NULL,
    source_run_ids_json       TEXT NOT NULL,
    superseded_run_ids_json   TEXT NOT NULL DEFAULT '[]',
    parser_version            TEXT NOT NULL,
    normalizer_version        TEXT NOT NULL,
    single_pdf_sha256         TEXT NOT NULL,
    page_count                INTEGER NOT NULL,
    block_count               INTEGER NOT NULL,
    searchable_block_count    INTEGER NOT NULL,
    artifact_path             TEXT NOT NULL,
    search_text_path          TEXT NOT NULL,
    status                    TEXT NOT NULL,
    warnings_json             TEXT NOT NULL DEFAULT '[]',
    created_at                TEXT NOT NULL,
    UNIQUE(document_id, source_signature)
);

CREATE INDEX IF NOT EXISTS idx_mofa_ocr_generations_document
    ON mofa_ocr_generations(document_id, created_at);

CREATE INDEX IF NOT EXISTS idx_mofa_ocr_generations_status
    ON mofa_ocr_generations(status);

CREATE TABLE IF NOT EXISTS mofa_ocr_active_generations (
    document_id    TEXT PRIMARY KEY REFERENCES documents(document_id) ON DELETE CASCADE,
    generation_id TEXT NOT NULL UNIQUE REFERENCES mofa_ocr_generations(generation_id) ON DELETE CASCADE,
    activated_at  TEXT NOT NULL
);
