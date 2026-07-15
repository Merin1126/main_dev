-- Versioned, user-maintainable query-expansion knowledge base for MOFA OCR search.

CREATE TABLE IF NOT EXISTS mofa_search_lexicon_rules (
    rule_id          TEXT PRIMARY KEY,
    category         TEXT NOT NULL
        CHECK(category IN ('glyph', 'ocr', 'alias', 'related')),
    source_term      TEXT NOT NULL,
    target_term      TEXT NOT NULL,
    source_norm      TEXT NOT NULL,
    target_norm      TEXT NOT NULL,
    bidirectional    INTEGER NOT NULL DEFAULT 1 CHECK(bidirectional IN (0, 1)),
    weight           REAL NOT NULL DEFAULT 1.0 CHECK(weight > 0 AND weight <= 1.0),
    active           INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    built_in         INTEGER NOT NULL DEFAULT 0 CHECK(built_in IN (0, 1)),
    notes            TEXT NOT NULL DEFAULT '',
    provenance       TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mofa_lexicon_rules_filter
    ON mofa_search_lexicon_rules(category, active, source_norm, target_norm);

CREATE TABLE IF NOT EXISTS mofa_search_lexicon_revisions (
    revision         INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash     TEXT NOT NULL UNIQUE,
    description      TEXT NOT NULL DEFAULT '',
    rule_count       INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mofa_search_lexicon_revision_rules (
    revision         INTEGER NOT NULL
        REFERENCES mofa_search_lexicon_revisions(revision) ON DELETE CASCADE,
    rule_id          TEXT NOT NULL,
    category         TEXT NOT NULL,
    source_term      TEXT NOT NULL,
    target_term      TEXT NOT NULL,
    source_norm      TEXT NOT NULL,
    target_norm      TEXT NOT NULL,
    bidirectional    INTEGER NOT NULL,
    weight           REAL NOT NULL,
    active           INTEGER NOT NULL,
    built_in         INTEGER NOT NULL,
    notes            TEXT NOT NULL DEFAULT '',
    provenance       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(revision, rule_id)
);

CREATE TABLE IF NOT EXISTS mofa_search_lexicon_state (
    state_id         INTEGER PRIMARY KEY CHECK(state_id = 1),
    current_revision INTEGER REFERENCES mofa_search_lexicon_revisions(revision),
    updated_at       TEXT NOT NULL
);

ALTER TABLE mofa_saved_searches
    ADD COLUMN expansion_level TEXT NOT NULL DEFAULT 'exact';
ALTER TABLE mofa_saved_searches
    ADD COLUMN lexicon_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mofa_saved_searches
    ADD COLUMN expansion_snapshot_json TEXT NOT NULL DEFAULT '{}';
