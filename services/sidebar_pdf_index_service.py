"""史料文件库侧栏：磁盘 PDF 列表与 SQLite 元数据索引、搜索。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from services.db_service import DbService
from utils.jacar_filename import extract_jacar_ref_from_path, parse_jacar_pdf_filename


@dataclass
class PdfListItem:
    path: str
    basename: str
    ref: str
    title: str
    level2: str
    parent: str
    repo: str
    keyword: str

    @property
    def line1(self) -> str:
        return self.ref or self.basename[:28]

    @property
    def line2(self) -> str:
        if self.title and self.parent:
            title = self._shorten(self.title, 22)
            parent = self._shorten(self.parent, 18)
            return f"{title} ｜ {parent}"
        if self.title:
            return self._shorten(self.title, 36)
        return self._shorten(self.basename, 36)

    @property
    def tooltip_text(self) -> str:
        return os.path.splitext(self.basename)[0]

    @property
    def search_blob(self) -> str:
        return " ".join(
            [
                self.basename,
                self.ref,
                self.title,
                self.level2,
                self.parent,
                self.repo,
                self.keyword,
            ]
        ).lower()

    @staticmethod
    def _shorten(text: str, max_len: int) -> str:
        text = (text or "").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"


class SidebarPdfIndexService:
    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
    ) -> None:
        root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.project_root = os.path.abspath(root)
        self.db_service = db_service or DbService()

    @staticmethod
    def _norm_path(path: str) -> str:
        return os.path.normpath(os.path.abspath(path))

    def _resolve_path(self, path: str) -> str:
        if not path:
            return ""
        if os.path.isabs(path):
            return self._norm_path(path)
        return self._norm_path(os.path.join(self.project_root, path))

    def _load_sql_by_ref(self) -> dict[str, dict[str, str]]:
        rows = self.db_service.fetchall(
            """
            SELECT
                UPPER(native_id) AS ref_key,
                COALESCE(native_id, '') AS native_id,
                COALESCE(title, '') AS title,
                COALESCE(level2_name, '') AS level2_name,
                COALESCE(parent_name, '') AS parent_name,
                COALESCE(repo_name, '') AS repo_name,
                COALESCE(NULLIF(TRIM(search_keyword), ''), '') AS search_keyword
            FROM documents
            WHERE source = 'jacar' AND status != 'failed'
            """
        )
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            key = str(row["ref_key"] or "").strip()
            if key:
                out[key] = dict(row)
        return out

    def build_index(self, pdf_paths: list[str]) -> dict[str, PdfListItem]:
        sql_by_ref = self._load_sql_by_ref()
        index: dict[str, PdfListItem] = {}
        for raw_path in pdf_paths:
            path = self._norm_path(raw_path)
            basename = os.path.basename(path)
            parts = parse_jacar_pdf_filename(path)
            ref = (parts.ref if parts else extract_jacar_ref_from_path(path) or "").strip()
            sql = sql_by_ref.get(ref.upper()) if ref else None
            if parts:
                title = parts.title
                level2 = parts.level2
                parent = parts.parent
                repo = parts.repo
            elif sql:
                title = str(sql.get("title") or "")
                level2 = str(sql.get("level2_name") or "")
                parent = str(sql.get("parent_name") or "")
                repo = str(sql.get("repo_name") or "")
            else:
                title = level2 = parent = repo = ""
            keyword = str(sql.get("search_keyword") or "") if sql else ""
            index[path] = PdfListItem(
                path=path,
                basename=basename,
                ref=ref,
                title=title,
                level2=level2,
                parent=parent,
                repo=repo,
                keyword=keyword,
            )
        return index

    def sql_search_paths(self, needle: str) -> set[str]:
        """用 SQLite 检索候选 PDF 绝对路径（存在且可解析）。"""
        text = (needle or "").strip()
        if not text:
            return set()
        pattern = f"%{text}%"
        rows = self.db_service.fetchall(
            """
            SELECT fp.path AS pdf_path
            FROM documents d
            LEFT JOIN files fp ON fp.document_id = d.document_id AND fp.kind = 'pdf'
            WHERE d.source = 'jacar' AND d.status != 'failed'
              AND (
                    UPPER(d.native_id) LIKE UPPER(?)
                 OR d.title LIKE ?
                 OR d.level2_name LIKE ?
                 OR d.parent_name LIKE ?
                 OR d.repo_name LIKE ?
                 OR d.search_keyword LIKE ?
                 OR fp.path LIKE ?
              )
            """,
            (pattern, pattern, pattern, pattern, pattern, pattern, pattern),
        )
        hits: set[str] = set()
        for row in rows:
            resolved = self._resolve_path(str(row["pdf_path"] or ""))
            if resolved and os.path.isfile(resolved):
                hits.add(resolved)
        return hits

    def filter_paths(
        self,
        index: dict[str, PdfListItem],
        needle: str,
    ) -> set[str]:
        text = (needle or "").strip()
        if not text:
            return set(index.keys())
        sql_hits = self.sql_search_paths(text)
        needle_lower = text.lower()
        hits: set[str] = set()
        for path, item in index.items():
            if path in sql_hits:
                hits.add(path)
                continue
            if needle_lower in item.search_blob:
                hits.add(path)
        return hits
