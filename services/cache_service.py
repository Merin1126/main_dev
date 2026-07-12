from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, List, Tuple

from config.settings import BUNDLE_REL_ANALYSIS, BUNDLE_REL_ANALYSIS_CONTEXT


class CacheService:
    """封装缓存路径、读写与目录清理。"""

    def build_cache_path(self, pdf_path: str, cache_dir: str) -> str:
        stat = os.stat(pdf_path)
        return self.build_cache_path_from_stat(
            pdf_path,
            cache_dir,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )

    @staticmethod
    def build_cache_path_from_stat(
        pdf_path: str,
        cache_dir: str,
        *,
        mtime_ns: int,
        size: int,
    ) -> str:
        """按路径 + mtime + size 生成缓存文件名（目标 PDF 尚未存在时也可计算新路径）。"""
        cache_key = f"{pdf_path}|{mtime_ns}|{size}"
        name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".txt"
        return os.path.join(cache_dir, name)

    def read_paged_cache(self, cache_path: str) -> List[str]:
        if not os.path.isfile(cache_path):
            return []
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            return []
        return self.parse_paged_text(raw)

    def write_paged_cache(self, cache_path: str, pages_list: List[str]) -> str:
        pages = ["" if p is None else str(p) for p in pages_list]
        payload = json.dumps({"format": "paged_v1", "pages": pages}, ensure_ascii=False)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(payload)
        return payload

    def clear_directory(self, dir_path: str) -> Tuple[int, int]:
        removed_count = 0
        failed_count = 0
        if not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
            return removed_count, failed_count
        for filename in os.listdir(dir_path):
            path = os.path.join(dir_path, filename)
            if not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                removed_count += 1
            except OSError:
                failed_count += 1
        return removed_count, failed_count

    @staticmethod
    def build_context_sidecar_path(cache_path: str) -> str:
        if os.path.basename(cache_path) == BUNDLE_REL_ANALYSIS:
            return os.path.join(os.path.dirname(cache_path), BUNDLE_REL_ANALYSIS_CONTEXT)
        base, _ext = os.path.splitext(cache_path)
        return f"{base}.context.json"

    def read_context_meta(self, cache_path: str) -> dict[str, Any]:
        sidecar_path = self.build_context_sidecar_path(cache_path)
        if not os.path.isfile(sidecar_path):
            return {}
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def write_context_meta(self, cache_path: str, payload: dict[str, Any]) -> str:
        sidecar_path = self.build_context_sidecar_path(cache_path)
        safe_payload = payload if isinstance(payload, dict) else {}
        encoded = json.dumps(safe_payload, ensure_ascii=False, indent=2)
        with open(sidecar_path, "w", encoding="utf-8") as f:
            f.write(encoded)
        return encoded

    def delete_context_meta(self, cache_path: str) -> bool:
        sidecar_path = self.build_context_sidecar_path(cache_path)
        if not os.path.exists(sidecar_path):
            return False
        try:
            os.remove(sidecar_path)
            return True
        except OSError:
            return False

    def parse_paged_text(self, cached_text: str) -> List[str]:
        try:
            payload = json.loads(cached_text)
            if isinstance(payload, dict) and payload.get("format") == "paged_v1":
                pages = payload.get("pages", [])
                if isinstance(pages, list) and len(pages) > 0:
                    return [str(p) for p in pages]
        except json.JSONDecodeError:
            pass

        legacy_pattern = r"\n\n===== 第 \d+ / \d+ 页 =====\n"
        parts = re.split(legacy_pattern, cached_text)
        page_texts = [part.strip() for part in parts if part.strip()]
        if page_texts:
            return page_texts
        return [cached_text.strip() if cached_text.strip() else "未识别到文本内容。"]
