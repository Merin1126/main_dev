"""HRS 全局 SQLite 访问服务。

设计要点：
- Singleton + 单一连接 + RLock，确保多线程抓取下写入串行化。
- 启用 WAL（写不阻塞读）+ NORMAL synchronous + foreign_keys=ON。
- 启动时自动应用 `database/schema/` 下的所有迁移脚本。
- 暴露 `execute` / `fetchone` / `fetchall` / `transaction` 等基础原语，
  具体的领域操作（documents / files / runs 等）由后续 repo 文件实现。
"""
from __future__ import annotations

import os
import json
import queue
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from database.migrations import apply_migrations


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DbService:
    """HRS 全局 SQLite 访问服务（Singleton）。"""

    _instance: "DbService | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls, db_path: str | None = None):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False  # type: ignore[attr-defined]
        return cls._instance

    def __init__(self, db_path: str | None = None) -> None:
        if getattr(self, "_initialized", False):
            return

        self._lock = threading.RLock()
        self._db_path = db_path or self.default_db_path()
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit；显式事务通过 BEGIN/COMMIT 控制
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row

        # 关键 PRAGMA：WAL + 外键 + 适度耐久性
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA temp_store=MEMORY")

        # 应用迁移
        schema_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "database",
            "schema",
        )
        apply_migrations(self._conn, schema_dir)

        # 下载事件采用异步入队，避免阻塞抓取下载线程
        self._event_queue: "queue.Queue[dict[str, Any] | None]" = queue.Queue(maxsize=10000)
        self._event_worker_stop = threading.Event()
        self._event_worker = threading.Thread(target=self._event_worker_loop, daemon=True)
        self._event_worker.start()

        self._initialized = True

    # ------------------------------------------------------------------
    # 基础元信息
    # ------------------------------------------------------------------
    @staticmethod
    def default_db_path() -> str:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, "database", "hrs.sqlite3")

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # ------------------------------------------------------------------
    # 事务 / 执行原语
    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：with db.transaction() as conn: ..."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, [tuple(p) for p in seq_of_params])

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    # ------------------------------------------------------------------
    # 时间戳辅助
    # ------------------------------------------------------------------
    @staticmethod
    def utc_now_iso() -> str:
        return _utc_now_iso()

    @staticmethod
    def local_now_iso() -> str:
        return _local_now_iso()

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def applied_schema_versions(self) -> list[tuple[int, str, str]]:
        """返回 schema_version 中已应用的版本列表（version, name, applied_at）。"""
        rows = self.fetchall("SELECT version, name, applied_at FROM schema_version ORDER BY version")
        return [(int(r["version"]), str(r["name"]), str(r["applied_at"])) for r in rows]

    def integrity_check(self) -> str:
        row = self.fetchone("PRAGMA integrity_check")
        return str(row[0]) if row else "unknown"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        worker = None
        with self._lock:
            if self._conn is None:
                return
            self._event_worker_stop.set()
            worker = self._event_worker
            # sentinel 放到队尾，尽量让已入队事件先落盘
            for _ in range(10):
                try:
                    self._event_queue.put(None, timeout=0.2)
                    break
                except queue.Full:
                    # 队列临时满：等待 worker 消费后重试
                    continue
                except Exception:
                    break

        if worker is not None and worker.is_alive():
            try:
                worker.join(timeout=2.0)
            except Exception:
                pass

        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None  # type: ignore[assignment]
                    self._initialized = False

    def _event_worker_loop(self) -> None:
        while True:
            try:
                payload = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                if self._event_worker_stop.is_set():
                    break
                continue
            if payload is None:
                break
            try:
                self._insert_download_event_sync(**payload)
            except Exception:
                # 事件审计失败不能影响主流程
                pass

    # ------------------------------------------------------------------
    # 领域辅助（阶段 2：抓取去重与状态闭环）
    # ------------------------------------------------------------------
    @staticmethod
    def make_document_id(source: str, native_id: str) -> str:
        return f"{source}:{native_id}"

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        s = str(value)
        digits = "".join(ch for ch in s if ch.isdigit())
        return int(digits) if digits else None

    def get_document_status(self, source: str, native_id: str) -> str | None:
        row = self.fetchone(
            "SELECT status FROM documents WHERE source = ? AND native_id = ? LIMIT 1",
            (source, native_id),
        )
        if not row:
            return None
        return str(row["status"]) if row["status"] is not None else None

    def add_document_keyword(self, source: str, native_id: str, keyword: str | None) -> None:
        """Record a keyword hit without changing document identity or download state."""
        clean = (keyword or "").strip()
        if not clean:
            return
        document_id = self.make_document_id(source, native_id)
        now = self.utc_now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM documents WHERE document_id = ? LIMIT 1",
                (document_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """
                INSERT INTO document_keywords(document_id, keyword, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id, keyword) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (document_id, clean, now, now),
            )

    def list_document_keywords(self, document_id: str) -> list[str]:
        rows = self.fetchall(
            "SELECT keyword FROM document_keywords WHERE document_id = ? ORDER BY keyword",
            (document_id,),
        )
        return [str(row["keyword"]) for row in rows]

    def upsert_document(
        self,
        *,
        source: str,
        native_id: str,
        title: str,
        repo_name: str | None = None,
        level2_name: str | None = None,
        parent_name: str | None = None,
        scale: Any = None,
        viewer_url: str | None = None,
        search_keyword: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "discovered",
    ) -> str:
        document_id = self.make_document_id(source, native_id)
        now = self.utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO documents(
                    document_id, source, native_id, title, repo_name, level2_name, parent_name,
                    scale, viewer_url, search_keyword, metadata_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    title=excluded.title,
                    repo_name=COALESCE(excluded.repo_name, documents.repo_name),
                    level2_name=COALESCE(excluded.level2_name, documents.level2_name),
                    parent_name=COALESCE(excluded.parent_name, documents.parent_name),
                    scale=COALESCE(excluded.scale, documents.scale),
                    viewer_url=COALESCE(excluded.viewer_url, documents.viewer_url),
                    search_keyword=COALESCE(excluded.search_keyword, documents.search_keyword),
                    metadata_json=COALESCE(excluded.metadata_json, documents.metadata_json),
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    document_id,
                    source,
                    native_id,
                    title,
                    repo_name,
                    level2_name,
                    parent_name,
                    self._safe_int(scale),
                    viewer_url,
                    search_keyword,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                    status,
                    now,
                    now,
                ),
            )
        self.add_document_keyword(source, native_id, search_keyword)
        return document_id

    def mark_document_status(self, source: str, native_id: str, status: str) -> None:
        now = self.utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = ?, updated_at = ?
                WHERE source = ? AND native_id = ?
                """,
                (status, now, source, native_id),
            )

    def _record_file(
        self,
        conn: sqlite3.Connection,
        *,
        document_id: str,
        kind: str,
        path: str,
    ) -> None:
        if not path:
            return
        if not os.path.exists(path):
            return
        st = os.stat(path)
        conn.execute(
            """
            INSERT INTO files(document_id, kind, path, size, mtime, sha256, verified_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(document_id, kind) DO UPDATE SET
                path = excluded.path,
                size = excluded.size,
                mtime = excluded.mtime,
                verified_at = excluded.verified_at
            """,
            (
                document_id,
                kind,
                path,
                int(st.st_size),
                int(st.st_mtime),
                self.utc_now_iso(),
            ),
        )

    def mark_downloaded_with_files(
        self,
        *,
        source: str,
        native_id: str,
        pdf_path: str | None = None,
        sidecar_path: str | None = None,
    ) -> None:
        document_id = self.make_document_id(source, native_id)
        now = self.utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = ?, updated_at = ?
                WHERE document_id = ?
                """,
                ("downloaded", now, document_id),
            )
            if pdf_path:
                self._record_file(conn, document_id=document_id, kind="pdf", path=pdf_path)
            if sidecar_path:
                self._record_file(conn, document_id=document_id, kind="sidecar", path=sidecar_path)

    def upsert_hoover_pending(
        self,
        *,
        native_id: str,
        title: str,
        viewer_url: str,
        metadata: dict[str, Any] | None = None,
        search_keyword: str | None = None,
        sidecar_path: str | None = None,
    ) -> str:
        document_id = self.upsert_document(
            source="hoover",
            native_id=native_id,
            title=title,
            viewer_url=viewer_url,
            search_keyword=search_keyword,
            metadata=metadata,
            status="pending_hoover",
        )
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO hoover_pending(document_id, viewer_url, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    viewer_url = excluded.viewer_url,
                    last_seen_at = excluded.last_seen_at
                """,
                (document_id, viewer_url, self.utc_now_iso()),
            )
            if sidecar_path:
                self._record_file(conn, document_id=document_id, kind="sidecar", path=sidecar_path)
        return document_id

    def _insert_download_event_sync(
        self,
        *,
        ref_code: str,
        event_type: str,
        message: str = "",
        run_id: int | None = None,
        source: str = "jacar",
    ) -> None:
        now = self.utc_now_iso()
        now_local = self.local_now_iso()
        document_id = self.make_document_id(source, ref_code) if ref_code else None
        if document_id is not None:
            row = self.fetchone("SELECT 1 FROM documents WHERE document_id = ? LIMIT 1", (document_id,))
            if row is None:
                document_id = None
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO download_events(
                    run_id, document_id, branch, status, bytes_downloaded, duration_ms, error_message, recorded_at,
                    ref_code, event_type, message, timestamp, timestamp_local
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    document_id,
                    source,
                    event_type,
                    message or "",
                    now,
                    ref_code,
                    event_type,
                    message or "",
                    now,
                    now_local,
                ),
            )

    def add_download_event(
        self,
        ref_code: str,
        event_type: str,
        message: str = "",
        *,
        run_id: int | None = None,
        source: str = "jacar",
    ) -> None:
        """异步记录下载轨迹；异常吞掉，不影响下载主流程。"""
        payload = {
            "ref_code": ref_code or "",
            "event_type": event_type or "",
            "message": message or "",
            "run_id": run_id,
            "source": source or "jacar",
        }
        try:
            self._event_queue.put_nowait(payload)
        except Exception:
            # 队列满时降级同步写；同步写也需异常保护
            try:
                self._insert_download_event_sync(**payload)
            except Exception:
                pass

    def begin_download_run(
        self,
        *,
        keyword: str,
        year_from: str,
        year_to: str,
        notes: str = "",
    ) -> int:
        now = self.utc_now_iso()
        now_local = self.local_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO download_runs(
                    started_at, started_at_local, keyword, year_from, year_to, notes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, now_local, keyword, year_from, year_to, notes),
            )
            return int(cur.lastrowid)

    def finish_download_run(
        self,
        run_id: int,
        *,
        dispatched: int,
        completed: int,
        succeeded: int,
        failed: int,
        sidecar_only: int,
        notes: str = "",
    ) -> None:
        now = self.utc_now_iso()
        now_local = self.local_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE download_runs
                SET finished_at = ?,
                    finished_at_local = ?,
                    dispatched = ?,
                    completed = ?,
                    succeeded = ?,
                    failed = ?,
                    sidecar_only = ?,
                    notes = CASE
                        WHEN ? = '' THEN notes
                        ELSE ?
                    END
                WHERE id = ?
                """,
                (
                    now,
                    now_local,
                    int(dispatched),
                    int(completed),
                    int(succeeded),
                    int(failed),
                    int(sidecar_only),
                    notes,
                    notes,
                    int(run_id),
                ),
            )

    def add_failed_row(
        self,
        *,
        run_id: int | None,
        reason: str,
        page_index: int | None,
        row_index: int | None,
        payload: dict[str, Any],
        ts: str | None = None,
    ) -> None:
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO failed_rows(run_id, ts, reason, page_index, row_index, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        ts or self.utc_now_iso(),
                        reason,
                        page_index,
                        row_index,
                        json.dumps(payload or {}, ensure_ascii=False),
                    ),
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 监控查询（阶段 4：GUI 监控弹窗由 DB 驱动）
    # ------------------------------------------------------------------
    def get_latest_run_id(self, prefer_active: bool = True) -> int | None:
        try:
            if prefer_active:
                row = self.fetchone(
                    "SELECT id FROM download_runs WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1"
                )
                if row:
                    return int(row["id"])
            row = self.fetchone("SELECT id FROM download_runs ORDER BY id DESC LIMIT 1")
            return int(row["id"]) if row else None
        except Exception:
            return None

    def get_run_summary(self, run_id: int) -> dict[str, Any] | None:
        try:
            row = self.fetchone(
                """
                SELECT id, started_at, started_at_local, finished_at, finished_at_local, keyword, year_from, year_to,
                       dispatched, completed, succeeded, failed, sidecar_only, notes
                FROM download_runs
                WHERE id = ?
                """,
                (int(run_id),),
            )
            return dict(row) if row else None
        except Exception:
            return None

    def get_run_monitor_rows(self, run_id: int) -> list[dict[str, Any]]:
        """返回某次 run 的任务监控行（documents JOIN latest download_events）。"""
        try:
            rows = self.fetchall(
                """
                WITH latest_events AS (
                    SELECT de.document_id, MAX(de.id) AS last_id
                    FROM download_events de
                    WHERE de.run_id = ? AND de.document_id IS NOT NULL
                    GROUP BY de.document_id
                )
                SELECT
                    d.document_id AS task_id,
                    d.title AS title,
                    d.status AS doc_status,
                    d.source AS source,
                    d.native_id AS native_id,
                    d.viewer_url AS viewer_url,
                    e.event_type AS event_type,
                    e.message AS message,
                    e.timestamp AS event_ts,
                    e.timestamp_local AS event_ts_local
                FROM latest_events le
                JOIN download_events e ON e.id = le.last_id
                JOIN documents d ON d.document_id = le.document_id
                ORDER BY e.id ASC
                """,
                (int(run_id),),
            )
            return [dict(r) for r in rows]
        except Exception:
            return []
