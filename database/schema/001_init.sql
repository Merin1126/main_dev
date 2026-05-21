-- ============================================================
-- HRS SQLite Schema 001: 阶段 1 防重与抓取审计
--
-- 表分工：
--   documents       : 史料逻辑实体（去重主键 source:native_id）
--   files           : 史料对应的物理文件（PDF / sidecar / IIIF 断点目录）
--   download_runs   : 每次抓取任务的元数据
--   download_events : 每条任务的下载事件流
--   failed_rows     : 失败行证据（替代 failed_rows_*.jsonl）
--   hoover_pending  : Hoover 待恢复登记表（替代 Hoover_Pending_Tasks.txt）
--
-- 注：schema_version 表由 database/migrations.py 自动维护，不在此文件创建。
-- ============================================================

CREATE TABLE IF NOT EXISTS documents (
    document_id    TEXT PRIMARY KEY,                       -- 逻辑主键，如 "jacar:B12345"
    source         TEXT NOT NULL,                          -- "jacar" | "toyo" | "hoover"
    native_id      TEXT NOT NULL,                          -- 源站 ID：Ref_Code / bbid / hoover_id
    title          TEXT,
    repo_name      TEXT,
    level2_name    TEXT,
    parent_name    TEXT,
    scale          INTEGER,
    viewer_url     TEXT,
    search_keyword TEXT,
    metadata_json  TEXT,                                   -- 全量 sidecar JSON 冗余备份
    status         TEXT NOT NULL DEFAULT 'discovered',     -- discovered | downloading | downloaded
                                                            -- | sidecar_only | failed | pending_hoover
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(source, native_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source_status ON documents(source, status);
CREATE INDEX IF NOT EXISTS idx_documents_keyword       ON documents(search_keyword);


CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                             -- "pdf" | "sidecar" | "iiif_resume"
    path        TEXT NOT NULL,
    size        INTEGER,
    mtime       INTEGER,
    sha256      TEXT,
    verified_at TEXT,
    UNIQUE(document_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_files_document ON files(document_id);
CREATE INDEX IF NOT EXISTS idx_files_kind     ON files(kind);


CREATE TABLE IF NOT EXISTS download_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    keyword      TEXT,
    year_from    TEXT,
    year_to      TEXT,
    dispatched   INTEGER NOT NULL DEFAULT 0,
    completed    INTEGER NOT NULL DEFAULT 0,
    succeeded    INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    sidecar_only INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);


CREATE TABLE IF NOT EXISTS download_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER REFERENCES download_runs(id),
    document_id      TEXT    REFERENCES documents(document_id),
    branch           TEXT,                                  -- "jacar_direct" | "iiif" | "hoover" | "sidecar_only"
    status           TEXT,                                  -- "queued" | "downloading" | "succeeded" | "failed" | "aborted"
    bytes_downloaded INTEGER,
    duration_ms      INTEGER,
    error_message    TEXT,
    recorded_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run      ON download_events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_document ON download_events(document_id);


CREATE TABLE IF NOT EXISTS failed_rows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES download_runs(id),
    ts           TEXT NOT NULL,
    reason       TEXT NOT NULL,
    page_index   INTEGER,
    row_index    INTEGER,
    payload_json TEXT
);


CREATE TABLE IF NOT EXISTS hoover_pending (
    document_id  TEXT PRIMARY KEY REFERENCES documents(document_id) ON DELETE CASCADE,
    viewer_url   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
