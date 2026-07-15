-- MOFA OCR page/block search index. FTS uses trigrams because Japanese OCR text
-- normally has no word separators. The ordinary tables remain the source of truth.

CREATE TABLE IF NOT EXISTS mofa_search_pages (
    page_row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id       TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    generation_id     TEXT NOT NULL REFERENCES mofa_ocr_generations(generation_id) ON DELETE CASCADE,
    native_id         TEXT NOT NULL,
    gregorian_year    INTEGER NOT NULL,
    volume_code       TEXT NOT NULL,
    title             TEXT NOT NULL,
    page_index        INTEGER NOT NULL,
    display_page      INTEGER NOT NULL,
    source_pdf_page   INTEGER,
    source_region     TEXT NOT NULL DEFAULT '',
    printed_page_label TEXT NOT NULL DEFAULT '',
    raw_text          TEXT NOT NULL DEFAULT '',
    search_text       TEXT NOT NULL DEFAULT '',
    UNIQUE(document_id, page_index)
);

CREATE INDEX IF NOT EXISTS idx_mofa_search_pages_generation
    ON mofa_search_pages(generation_id);
CREATE INDEX IF NOT EXISTS idx_mofa_search_pages_filters
    ON mofa_search_pages(gregorian_year, volume_code, document_id);

CREATE TABLE IF NOT EXISTS mofa_search_blocks (
    block_row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    block_key         TEXT NOT NULL UNIQUE,
    page_row_id       INTEGER NOT NULL REFERENCES mofa_search_pages(page_row_id) ON DELETE CASCADE,
    document_id       TEXT NOT NULL,
    generation_id     TEXT NOT NULL,
    page_index        INTEGER NOT NULL,
    block_order       INTEGER NOT NULL,
    block_type        TEXT NOT NULL,
    bbox_json         TEXT,
    raw_text          TEXT NOT NULL DEFAULT '',
    search_text       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_mofa_search_blocks_page
    ON mofa_search_blocks(document_id, page_index, block_order);

CREATE VIRTUAL TABLE IF NOT EXISTS mofa_search_pages_fts USING fts5(
    raw_text,
    search_text,
    content='mofa_search_pages',
    content_rowid='page_row_id',
    tokenize='trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS mofa_search_blocks_fts USING fts5(
    raw_text,
    search_text,
    content='mofa_search_blocks',
    content_rowid='block_row_id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS mofa_search_pages_ai AFTER INSERT ON mofa_search_pages BEGIN
    INSERT INTO mofa_search_pages_fts(rowid, raw_text, search_text)
    VALUES (new.page_row_id, new.raw_text, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS mofa_search_pages_ad AFTER DELETE ON mofa_search_pages BEGIN
    INSERT INTO mofa_search_pages_fts(mofa_search_pages_fts, rowid, raw_text, search_text)
    VALUES ('delete', old.page_row_id, old.raw_text, old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS mofa_search_pages_au AFTER UPDATE ON mofa_search_pages BEGIN
    INSERT INTO mofa_search_pages_fts(mofa_search_pages_fts, rowid, raw_text, search_text)
    VALUES ('delete', old.page_row_id, old.raw_text, old.search_text);
    INSERT INTO mofa_search_pages_fts(rowid, raw_text, search_text)
    VALUES (new.page_row_id, new.raw_text, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS mofa_search_blocks_ai AFTER INSERT ON mofa_search_blocks BEGIN
    INSERT INTO mofa_search_blocks_fts(rowid, raw_text, search_text)
    VALUES (new.block_row_id, new.raw_text, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS mofa_search_blocks_ad AFTER DELETE ON mofa_search_blocks BEGIN
    INSERT INTO mofa_search_blocks_fts(mofa_search_blocks_fts, rowid, raw_text, search_text)
    VALUES ('delete', old.block_row_id, old.raw_text, old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS mofa_search_blocks_au AFTER UPDATE ON mofa_search_blocks BEGIN
    INSERT INTO mofa_search_blocks_fts(mofa_search_blocks_fts, rowid, raw_text, search_text)
    VALUES ('delete', old.block_row_id, old.raw_text, old.search_text);
    INSERT INTO mofa_search_blocks_fts(rowid, raw_text, search_text)
    VALUES (new.block_row_id, new.raw_text, new.search_text);
END;

CREATE TABLE IF NOT EXISTS mofa_fts_index_state (
    document_id    TEXT PRIMARY KEY REFERENCES documents(document_id) ON DELETE CASCADE,
    generation_id  TEXT NOT NULL REFERENCES mofa_ocr_generations(generation_id) ON DELETE CASCADE,
    page_count      INTEGER NOT NULL,
    block_count     INTEGER NOT NULL,
    indexed_at      TEXT NOT NULL
);
