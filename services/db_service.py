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
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from database.migrations import apply_migrations


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None  # type: ignore[assignment]
                    self._initialized = False
