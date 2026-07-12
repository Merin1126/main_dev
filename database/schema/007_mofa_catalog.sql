CREATE TABLE IF NOT EXISTS mofa_catalog_items (
    native_id       TEXT PRIMARY KEY,
    gregorian_year  INTEGER NOT NULL,
    era_code        TEXT NOT NULL,
    era_year        INTEGER NOT NULL,
    volume_code     TEXT NOT NULL,
    volume_label    TEXT NOT NULL,
    title           TEXT NOT NULL,
    item_kind       TEXT NOT NULL,
    catalog_url     TEXT NOT NULL,
    pdf_url         TEXT NOT NULL UNIQUE,
    item_order      INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mofa_catalog_year_volume
    ON mofa_catalog_items(gregorian_year, volume_code, item_order);

CREATE INDEX IF NOT EXISTS idx_mofa_catalog_kind
    ON mofa_catalog_items(item_kind);
