-- Phase 3: 史料元数据变更审计（重命名 / 目录编辑）

CREATE TABLE IF NOT EXISTS document_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL,
    native_id       TEXT,
    action          TEXT NOT NULL,
    changes_json    TEXT NOT NULL,
    pdf_path_before TEXT,
    pdf_path_after  TEXT,
    source          TEXT NOT NULL DEFAULT 'catalog_ui',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_audit_document_id
    ON document_audit(document_id);

CREATE INDEX IF NOT EXISTS idx_document_audit_created_at
    ON document_audit(created_at DESC);
