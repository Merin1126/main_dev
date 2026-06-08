"""史料元数据变更审计日志（document_audit 表）。"""
from __future__ import annotations

import json
from typing import Any

from services.db_service import DbService
from utils.jacar_filename import JacarFilenameParts


class DocumentAuditService:
    def __init__(self, db_service: DbService | None = None) -> None:
        self.db_service = db_service or DbService()

    def log_rename(
        self,
        *,
        document_id: str,
        native_id: str,
        before: dict[str, str],
        after: dict[str, str],
        pdf_path_before: str,
        pdf_path_after: str,
        source: str = "catalog_ui",
        action: str = "rename_metadata",
    ) -> None:
        changes = {
            "before": before,
            "after": after,
        }
        self.db_service.execute(
            """
            INSERT INTO document_audit (
                document_id, native_id, action, changes_json,
                pdf_path_before, pdf_path_after, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                native_id,
                action,
                json.dumps(changes, ensure_ascii=False),
                pdf_path_before,
                pdf_path_after,
                source,
                self.db_service.utc_now_iso(),
            ),
        )

    @staticmethod
    def parts_snapshot(parts: JacarFilenameParts) -> dict[str, str]:
        return {
            "level2": parts.level2,
            "title": parts.title,
            "ref": parts.ref,
            "parent": parts.parent,
            "repo": parts.repo,
            "image_range": parts.image_range,
        }

    def fetch_for_document(
        self,
        document_id: str,
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        rows = self.db_service.fetchall(
            """
            SELECT id, document_id, native_id, action, changes_json,
                   pdf_path_before, pdf_path_after, source, created_at
            FROM document_audit
            WHERE document_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (document_id, max(1, int(limit))),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw = item.pop("changes_json", None)
            try:
                item["changes"] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                item["changes"] = {}
            out.append(item)
        return out

    def format_log_lines(self, records: list[dict[str, Any]]) -> str:
        if not records:
            return "（暂无变更记录）"
        lines: list[str] = []
        for rec in records:
            ts = rec.get("created_at", "")
            action = rec.get("action", "")
            src = rec.get("source", "")
            changes = rec.get("changes") or {}
            before = changes.get("before") or {}
            after = changes.get("after") or {}
            lines.append(f"—— {ts}  [{action}]  {src}")
            for key in ("level2", "title", "parent", "repo"):
                b = before.get(key, "")
                a = after.get(key, "")
                if b != a:
                    lines.append(f"  {key}: {b!r} → {a!r}")
            old_p = rec.get("pdf_path_before") or ""
            new_p = rec.get("pdf_path_after") or ""
            if old_p != new_p:
                import os

                lines.append(f"  pdf: {os.path.basename(old_p)} → {os.path.basename(new_p)}")
        return "\n".join(lines)
