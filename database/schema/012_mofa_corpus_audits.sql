CREATE TABLE IF NOT EXISTS mofa_corpus_audit_runs (
    audit_id            TEXT PRIMARY KEY,
    scope_label         TEXT NOT NULL,
    status              TEXT NOT NULL,
    entry_count         INTEGER NOT NULL,
    healthy_count       INTEGER NOT NULL,
    issue_count         INTEGER NOT NULL,
    duration_ms         INTEGER NOT NULL,
    database_size_bytes INTEGER NOT NULL,
    summary_json        TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mofa_audit_runs_started
    ON mofa_corpus_audit_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS mofa_corpus_audit_issues (
    issue_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id       TEXT NOT NULL REFERENCES mofa_corpus_audit_runs(audit_id) ON DELETE CASCADE,
    document_id    TEXT REFERENCES documents(document_id) ON DELETE SET NULL,
    native_id      TEXT NOT NULL,
    title          TEXT NOT NULL,
    severity       TEXT NOT NULL,
    stage          TEXT NOT NULL,
    code           TEXT NOT NULL,
    repair_action  TEXT NOT NULL DEFAULT '',
    message        TEXT NOT NULL,
    details_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_mofa_audit_issues_run
    ON mofa_corpus_audit_issues(audit_id, severity, stage);
CREATE INDEX IF NOT EXISTS idx_mofa_audit_issues_document
    ON mofa_corpus_audit_issues(native_id, code);
