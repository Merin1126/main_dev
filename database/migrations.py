"""HRS SQLite 迁移执行器。

约定：
- `database/schema/` 下的 SQL 文件命名为 `NNN_name.sql`（如 `001_init.sql`）。
- 文件按版本号升序顺序应用，已应用版本记录在 `schema_version` 表中。
- 每个迁移文件应当幂等（建议使用 `CREATE TABLE IF NOT EXISTS` 等语句），以便重复执行不破坏数据。
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone

_MIGRATION_PATTERN = re.compile(r"^(\d+)_([a-zA-Z0-9_]+)\.sql$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def discover_migrations(schema_dir: str) -> list[tuple[int, str, str]]:
    """扫描迁移目录，返回按版本号升序的 (version, name, path) 列表。"""
    items: list[tuple[int, str, str]] = []
    if not os.path.isdir(schema_dir):
        return items
    for entry in sorted(os.listdir(schema_dir)):
        m = _MIGRATION_PATTERN.match(entry)
        if not m:
            continue
        version = int(m.group(1))
        name = m.group(2)
        items.append((version, name, os.path.join(schema_dir, entry)))
    items.sort(key=lambda x: x[0])
    return items


def _is_ignorable_migration_error(stmt: str, err: sqlite3.OperationalError) -> bool:
    """仅对已知可恢复的迁移异常降级处理（如重复加列）。"""
    msg = str(err).lower()
    normalized = stmt.strip().upper()
    if "duplicate column name" in msg and "ALTER TABLE" in normalized and "ADD COLUMN" in normalized:
        return True
    return False


def _execute_migration_sql(conn: sqlite3.Connection, sql: str) -> None:
    """
    顺序执行 migration SQL。
    - 使用 sqlite3.complete_statement 做语句切分；
    - 遇到「重复加列」这类幂等冲突时忽略并继续，保证后续 UPDATE 可执行。
    """
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if not sqlite3.complete_statement(buf):
            continue
        stmt = buf.strip()
        buf = ""
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if _is_ignorable_migration_error(stmt, e):
                continue
            raise
    trailing = buf.strip()
    if trailing:
        conn.execute(trailing)


def apply_migrations(conn: sqlite3.Connection, schema_dir: str) -> list[int]:
    """按版本顺序应用迁移脚本，返回本次新应用的版本号列表。

    幂等保证：已应用的版本号会跳过；schema_version 表会自动创建。
    """
    _ensure_schema_version_table(conn)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version").fetchall()}

    newly_applied: list[int] = []
    for version, name, path in discover_migrations(schema_dir):
        if version in applied:
            continue
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        _execute_migration_sql(conn, sql)
        conn.execute(
            "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, _utc_now_iso()),
        )
        newly_applied.append(version)
    return newly_applied
