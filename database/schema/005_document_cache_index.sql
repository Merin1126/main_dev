-- Phase 5: 缓存文件 ↔ JACAR Ref 反向索引（OCR / Analysis / Translation）

CREATE TABLE IF NOT EXISTS document_cache_index (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT,
    native_id       TEXT NOT NULL DEFAULT '',
    cache_kind      TEXT NOT NULL,
    cache_path      TEXT NOT NULL,
    cache_basename  TEXT NOT NULL,
    pdf_path        TEXT NOT NULL DEFAULT '',
    cache_key       TEXT NOT NULL DEFAULT '',
    pdf_mtime       INTEGER,
    pdf_size        INTEGER,
    is_present      INTEGER NOT NULL DEFAULT 1,
    is_orphan       INTEGER NOT NULL DEFAULT 0,
    indexed_at      TEXT NOT NULL,
    UNIQUE(cache_kind, cache_basename)
);

CREATE INDEX IF NOT EXISTS idx_document_cache_index_native_id
    ON document_cache_index(native_id);

CREATE INDEX IF NOT EXISTS idx_document_cache_index_document_id
    ON document_cache_index(document_id);

CREATE INDEX IF NOT EXISTS idx_document_cache_index_basename
    ON document_cache_index(cache_basename);

CREATE INDEX IF NOT EXISTS idx_document_cache_index_pdf_path
    ON document_cache_index(pdf_path);
