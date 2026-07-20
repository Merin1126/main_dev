-- Phase 7A: MOFA-specific research work packages built from page candidates.

CREATE TABLE IF NOT EXISTS mofa_research_packages (
    package_id          TEXT PRIMARY KEY,
    package_signature   TEXT NOT NULL UNIQUE,
    source              TEXT NOT NULL DEFAULT 'mofa' CHECK(source = 'mofa'),
    package_type        TEXT NOT NULL DEFAULT 'mofa_research_package'
        CHECK(package_type = 'mofa_research_package'),
    display_name        TEXT NOT NULL,
    package_status      TEXT NOT NULL DEFAULT 'draft'
        CHECK(package_status IN ('draft', 'ready', 'processing', 'completed', 'archived')),
    relative_dir        TEXT NOT NULL UNIQUE,
    manifest_filename   TEXT NOT NULL DEFAULT 'mofa_research_package.json',
    context_before      INTEGER NOT NULL DEFAULT 0 CHECK(context_before >= 0),
    context_after       INTEGER NOT NULL DEFAULT 0 CHECK(context_after >= 0),
    document_count      INTEGER NOT NULL DEFAULT 0,
    range_count         INTEGER NOT NULL DEFAULT 0,
    selected_page_count INTEGER NOT NULL DEFAULT 0,
    included_page_count INTEGER NOT NULL DEFAULT 0,
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mofa_research_packages_status
    ON mofa_research_packages(package_status, created_at);

CREATE TABLE IF NOT EXISTS mofa_research_package_ranges (
    range_id              TEXT PRIMARY KEY,
    package_id            TEXT NOT NULL
        REFERENCES mofa_research_packages(package_id) ON DELETE CASCADE,
    document_id           TEXT NOT NULL
        REFERENCES documents(document_id) ON DELETE RESTRICT,
    native_id             TEXT NOT NULL,
    generation_id         TEXT NOT NULL
        REFERENCES mofa_ocr_generations(generation_id) ON DELETE RESTRICT,
    gregorian_year        INTEGER NOT NULL,
    volume_code           TEXT NOT NULL,
    title                 TEXT NOT NULL,
    start_page_index      INTEGER NOT NULL CHECK(start_page_index >= 0),
    end_page_index        INTEGER NOT NULL CHECK(end_page_index >= start_page_index),
    selected_pages_json   TEXT NOT NULL DEFAULT '[]',
    included_page_count   INTEGER NOT NULL,
    range_order           INTEGER NOT NULL,
    UNIQUE(package_id, document_id, start_page_index, end_page_index)
);

CREATE INDEX IF NOT EXISTS idx_mofa_research_ranges_package
    ON mofa_research_package_ranges(package_id, range_order);

CREATE TABLE IF NOT EXISTS mofa_research_package_candidates (
    package_id   TEXT NOT NULL
        REFERENCES mofa_research_packages(package_id) ON DELETE CASCADE,
    range_id     TEXT NOT NULL
        REFERENCES mofa_research_package_ranges(range_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL
        REFERENCES mofa_research_candidates(candidate_id) ON DELETE RESTRICT,
    page_index   INTEGER NOT NULL,
    PRIMARY KEY(package_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_mofa_research_package_candidates_range
    ON mofa_research_package_candidates(range_id, page_index);
