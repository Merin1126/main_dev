"""根据已存在的 PDF 文件名，同步 JACAR_Downloads 内配对 sidecar JSON 的路径与标题字段。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from utils.jacar_filename import extract_jacar_ref_from_path, parse_jacar_pdf_filename
from utils.jacar_sidecar import patch_jacar_download_sidecar, sidecar_path_for_pdf


@dataclass
class SidecarFilenameSyncStats:
    pdfs_seen: int = 0
    json_renamed: int = 0
    json_content_patched: int = 0
    already_matched: int = 0
    pdfs_without_json: int = 0
    json_without_pdf: int = 0
    conflicts: int = 0
    notes: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        return [
            f"扫描 PDF：{self.pdfs_seen} 个",
            f"已重命名 JSON 以匹配 PDF：{self.json_renamed} 个",
            f"已更新 JSON 内 Title 等字段：{self.json_content_patched} 个",
            f"本就与 PDF 同名：{self.already_matched} 个",
            f"有 PDF 无配对 JSON：{self.pdfs_without_json} 个",
            f"有 JSON 找不到对应 PDF（按 Ref）：{self.json_without_pdf} 个",
            f"冲突（目标 JSON 已存在等）：{self.conflicts} 个",
            *(
                ["", "备注：", *[f"  · {n}" for n in self.notes[:25]]]
                if self.notes
                else []
            ),
        ]


def _ref_from_sidecar_json(path: str) -> str:
    ref = extract_jacar_ref_from_path(path)
    if ref:
        return ref.upper()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in ("Ref_Code", "レファレンスコード", "ref_code"):
                val = data.get(key)
                if val:
                    return str(val).strip().upper()
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def _collect_files(root: str, suffix: str) -> list[str]:
    out: list[str] = []
    for base, _, names in os.walk(root):
        for name in names:
            if name.lower().endswith(suffix):
                out.append(os.path.join(base, name))
    out.sort()
    return out


class SidecarFilenameSyncService:
    def __init__(self, *, project_root: str | None = None) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.downloads_root = os.path.join(self.project_root, "JACAR_Downloads")

    def sync_directory(
        self,
        target_dir: str | None = None,
        *,
        patch_json_content: bool = True,
    ) -> SidecarFilenameSyncStats:
        """
        将 target_dir（默认整个 JACAR_Downloads）内 sidecar JSON 文件名对齐到同 Ref 的 PDF。
        适用于用户已在 Finder 中改过 PDF 名、JSON 仍为旧文件名的情况。
        """
        root = os.path.abspath(target_dir or self.downloads_root)
        stats = SidecarFilenameSyncStats()
        if not os.path.isdir(root):
            stats.notes.append(f"目录不存在：{root}")
            return stats

        pdf_paths = _collect_files(root, ".pdf")
        json_paths = _collect_files(root, ".json")
        stats.pdfs_seen = len(pdf_paths)

        ref_to_pdf: dict[str, str] = {}
        for pdf in pdf_paths:
            ref = extract_jacar_ref_from_path(pdf)
            if not ref:
                continue
            key = ref.upper()
            if key in ref_to_pdf and ref_to_pdf[key] != pdf:
                stats.conflicts += 1
                stats.notes.append(
                    f"Ref {ref} 对应多份 PDF，已使用：{os.path.basename(pdf)}"
                )
            ref_to_pdf[key] = pdf

        ref_to_jsons: dict[str, list[str]] = {}
        for jp in json_paths:
            ref = _ref_from_sidecar_json(jp)
            if not ref:
                continue
            ref_to_jsons.setdefault(ref, []).append(jp)

        used_json: set[str] = set()

        for ref, pdf_path in ref_to_pdf.items():
            target_json = sidecar_path_for_pdf(pdf_path)
            candidates = [p for p in ref_to_jsons.get(ref, []) if p not in used_json]

            if not candidates:
                stats.pdfs_without_json += 1
                continue

            # 优先：与 PDF 同目录；其次：路径已是目标名
            candidates.sort(
                key=lambda p: (
                    0 if os.path.dirname(p) == os.path.dirname(pdf_path) else 1,
                    0 if os.path.normpath(p) == os.path.normpath(target_json) else 1,
                    len(os.path.basename(p)),
                )
            )
            source_json = candidates[0]
            used_json.add(source_json)

            if os.path.normpath(source_json) == os.path.normpath(target_json):
                stats.already_matched += 1
                if patch_json_content:
                    if self._try_patch_sidecar(pdf_path, target_json):
                        stats.json_content_patched += 1
                continue

            if os.path.isfile(target_json):
                stats.conflicts += 1
                stats.notes.append(
                    f"无法重命名 {os.path.basename(source_json)}：目标已存在 {os.path.basename(target_json)}"
                )
                continue

            try:
                os.rename(source_json, target_json)
                stats.json_renamed += 1
                used_json.add(target_json)
                if patch_json_content and self._try_patch_sidecar(pdf_path, target_json):
                    stats.json_content_patched += 1
            except OSError as exc:
                stats.conflicts += 1
                stats.notes.append(f"重命名失败 {os.path.basename(source_json)}：{exc}")

        for ref, paths in ref_to_jsons.items():
            for jp in paths:
                if jp in used_json:
                    continue
                if ref not in ref_to_pdf:
                    stats.json_without_pdf += 1

        return stats

    @staticmethod
    def _try_patch_sidecar(pdf_path: str, sidecar_path: str) -> bool:
        parts = parse_jacar_pdf_filename(pdf_path)
        if parts is None:
            return False
        try:
            patch_jacar_download_sidecar(sidecar_path, parts)
            return True
        except (OSError, json.JSONDecodeError, TypeError):
            return False
