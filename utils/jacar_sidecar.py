"""JACAR_Downloads 内 PDF 配对 sidecar JSON 的路径与字段更新。"""
from __future__ import annotations

import json
import os

from utils.jacar_filename import JacarFilenameParts


def sidecar_path_for_pdf(pdf_path: str) -> str:
    """与 core_scraper 一致：PDF 主文件名 + .json。"""
    return os.path.splitext(os.path.abspath(pdf_path))[0] + ".json"


def patch_jacar_download_sidecar(sidecar_path: str, parts: JacarFilenameParts) -> None:
    with open(sidecar_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
    data["Title"] = parts.title
    data["Ref_Code"] = parts.ref
    data["レファレンスコード"] = parts.ref
    data["Level2_Name"] = parts.level2
    data["Parent_Name"] = parts.parent
    data["Repo_Name"] = parts.repo
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
