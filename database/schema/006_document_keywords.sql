-- 一份史料可由多个检索词命中；documents.search_keyword 暂保留为首要/兼容字段。
CREATE TABLE IF NOT EXISTS document_keywords (
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    keyword     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY(document_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_document_keywords_keyword
    ON document_keywords(keyword);

INSERT OR IGNORE INTO document_keywords(document_id, keyword, first_seen_at, last_seen_at)
SELECT document_id, TRIM(search_keyword), created_at, updated_at
FROM documents
WHERE NULLIF(TRIM(search_keyword), '') IS NOT NULL;
