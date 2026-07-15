CREATE TABLE IF NOT EXISTS mofa_saved_searches (
    search_id         TEXT PRIMARY KEY,
    search_signature  TEXT NOT NULL UNIQUE,
    query_text        TEXT NOT NULL,
    normalized_query  TEXT NOT NULL,
    search_mode       TEXT NOT NULL,
    year_filter       INTEGER,
    volume_filter     TEXT NOT NULL DEFAULT '',
    result_count      INTEGER NOT NULL DEFAULT 0,
    saved_at          TEXT NOT NULL,
    last_used_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mofa_research_candidates (
    candidate_id       TEXT PRIMARY KEY,
    document_id        TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    native_id          TEXT NOT NULL,
    generation_id      TEXT NOT NULL REFERENCES mofa_ocr_generations(generation_id) ON DELETE CASCADE,
    gregorian_year     INTEGER NOT NULL,
    volume_code        TEXT NOT NULL,
    title              TEXT NOT NULL,
    page_index         INTEGER NOT NULL,
    display_page       INTEGER NOT NULL,
    source_pdf_page    INTEGER,
    source_region      TEXT NOT NULL DEFAULT '',
    printed_page_label TEXT NOT NULL DEFAULT '',
    raw_text           TEXT NOT NULL DEFAULT '',
    research_status    TEXT NOT NULL DEFAULT 'candidate'
        CHECK(research_status IN ('candidate', 'relevant', 'excluded')),
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(document_id, page_index)
);

CREATE INDEX IF NOT EXISTS idx_mofa_candidates_status
    ON mofa_research_candidates(research_status, gregorian_year, volume_code);
CREATE INDEX IF NOT EXISTS idx_mofa_candidates_native
    ON mofa_research_candidates(native_id, page_index);

CREATE TABLE IF NOT EXISTS mofa_candidate_search_sources (
    candidate_id  TEXT NOT NULL REFERENCES mofa_research_candidates(candidate_id) ON DELETE CASCADE,
    search_id     TEXT NOT NULL REFERENCES mofa_saved_searches(search_id) ON DELETE CASCADE,
    score         REAL NOT NULL DEFAULT 0,
    recorded_at   TEXT NOT NULL,
    PRIMARY KEY(candidate_id, search_id)
);

CREATE TABLE IF NOT EXISTS mofa_candidate_blocks (
    candidate_id  TEXT NOT NULL REFERENCES mofa_research_candidates(candidate_id) ON DELETE CASCADE,
    block_key     TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    block_order   INTEGER NOT NULL,
    block_type    TEXT NOT NULL,
    bbox_json     TEXT,
    raw_text      TEXT NOT NULL DEFAULT '',
    search_text   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(candidate_id, block_key)
);

CREATE INDEX IF NOT EXISTS idx_mofa_candidate_blocks_candidate
    ON mofa_candidate_blocks(candidate_id, block_order);

CREATE TABLE IF NOT EXISTS mofa_candidate_tags (
    candidate_id TEXT NOT NULL REFERENCES mofa_research_candidates(candidate_id) ON DELETE CASCADE,
    tag          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY(candidate_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_mofa_candidate_tags_tag
    ON mofa_candidate_tags(tag, candidate_id);
