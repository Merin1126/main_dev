"""将 SQLite documents 全量元数据注入汇报用静态 HTML 演示页。

默认富化范围（--enrich / --release）：
    仅「反帝國主義」专题：在 JACAR_Downloads/反帝國主義 下拼接 PDF 预览、OCR、Analysis JSON、summary。
    其余专题只注入目录条目（cat/type/title/ref/coll），不写 details。

用法（在项目根目录执行）：
    python scripts/inject_db_to_html.py
    python scripts/inject_db_to_html.py --enrich
    python scripts/inject_db_to_html.py --release
    python scripts/inject_db_to_html.py --db-path database/hrs.sqlite3 --html-path Reports/HRS_Database_Demo.html

富化时建议安装 Markdown（生成 summary_html；否则页面用 marked.js 渲染 summary）：
    pip install markdown

输出：
    Reports/HRS_Database_Report_YYYYMMDD_HHMMSS.html
    （富化专题另生成 Reports/assets/<ref>/page_*.jpg；--max-preview-pages 0 表示 PDF 全页）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.cache_service import CacheService  # noqa: E402
from services.db_service import DbService  # noqa: E402

logger = logging.getLogger(__name__)

_TRANSCRIPTION_RE = re.compile(
    r"<transcription>\s*(.*?)\s*</transcription>",
    flags=re.IGNORECASE | re.DOTALL,
)


def extract_transcription_text(text: str) -> str:
    """从 OCR 页文本中抽取 <transcription> 内日文，逻辑与 utils.reporting 一致。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _TRANSCRIPTION_RE.search(raw)
    return (match.group(1) if match else raw).strip()

DEFAULT_HTML = _PROJECT_ROOT / "Reports" / "HRS_Database_Demo.html"
DEFAULT_HTML_DRAWER = _PROJECT_ROOT / "Reports" / "HRS_Database_Demo2.html"
DEFAULT_RELEASE_HTML = _PROJECT_ROOT / "Reports" / "HRS_Final_Release.html"
_RAW_DATA_MARKER = re.compile(r"const\s+rawData\s*=\s*\[")
_JSON_PAGE_RE = re.compile(r"_p(\d+)\.json$", re.IGNORECASE)
_SAFE_REF_RE = re.compile(r"[^A-Za-z0-9._-]+")

# PDF 默认 72pt≈72DPI；zoom=2 约 144DPI。与 utils.docx_export 对照报告一致用 240DPI。
_PDF_BASE_DPI = 72.0
DEFAULT_PREVIEW_DPI = 240.0
DEFAULT_PREVIEW_MAX_PIXELS = 4800
DEFAULT_JPEG_QUALITY = 92
# 0 表示不限制页数，渲染 PDF 全部页面
DEFAULT_MAX_PREVIEW_PAGES = 0

# 默认仅富化该话语主题（对应 documents.search_keyword / rawData.cat）
DEFAULT_ENRICH_CAT = "反帝國主義"
# 仅在此子目录下查找 PDF（相对 JACAR_Downloads）
DEFAULT_ENRICH_DOWNLOADS_SUBDIR = "反帝國主義"


def build_cache_path(pdf_path: str, cache_dir: str) -> str:
    """与 CacheService.build_cache_path 完全一致：PDF 路径 + mtime + size → SHA256 文件名。"""
    stat = os.stat(pdf_path)
    cache_key = f"{pdf_path}|{stat.st_mtime_ns}|{stat.st_size}"
    name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".txt"
    return os.path.join(cache_dir, name)


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(DbService.default_db_path())


def _safe_ref_dirname(ref: str) -> str:
    """将 JACAR 编号转为可作目录名的字符串。"""
    cleaned = _SAFE_REF_RE.sub("_", (ref or "").strip())
    return cleaned or "unknown_ref"


def _item_in_enrich_scope(item: dict[str, Any], enrich_cat: str) -> bool:
    """是否对该条史料执行 PDF/OCR/Analysis/summary 富化。"""
    return str(item.get("cat") or "").strip() == str(enrich_cat or "").strip()


def _resolve_preview_page_cap(max_preview_pages: int, doc_page_count: int) -> int:
    """将 max_preview_pages 转为实际渲染页数；<=0 表示全页。"""
    total = max(0, int(doc_page_count))
    cap = int(max_preview_pages)
    if cap <= 0:
        return total
    return min(cap, total)


def fetch_documents(db_path: Path) -> list[dict[str, Any]]:
    """从 SQLite documents 表读取汇报用基础元数据。"""
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(search_keyword), ''), '未分类') AS cat,
                COALESCE(NULLIF(TRIM(repo_name), ''), NULLIF(TRIM(source), ''), '未知') AS type,
                COALESCE(title, '') AS title,
                native_id AS ref,
                COALESCE(parent_name, '') AS coll
            FROM documents
            WHERE status != 'failed'
            ORDER BY cat, type, ref
            """
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        {
            "cat": str(row["cat"] or "未分类"),
            "type": str(row["type"] or "未知"),
            "title": str(row["title"] or ""),
            "ref": str(row["ref"] or ""),
            "coll": str(row["coll"] or ""),
        }
        for row in rows
    ]


def _empty_details() -> dict[str, Any]:
    """富化失败或部分缺失时的默认 details 结构。"""
    return {
        "summary": "",
        "summary_html": "",
        "pdf_path": "",
        "ocr_cache_path": "",
        "pages_data": [],
    }


def _markdown_to_html(text: str) -> str:
    """将 summary.md 转为 HTML，供抽屉左侧直接 innerHTML 渲染。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        import markdown

        return markdown.markdown(raw, extensions=["extra", "nl2br"])
    except Exception:
        try:
            import markdown

            return markdown.markdown(raw)
        except Exception as exc:
            logger.warning("Markdown 转换失败，回退纯文本: %s", exc)
            escaped = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f"<pre class='text-sm whitespace-pre-wrap'>{escaped}</pre>"


def _find_summary_file(summary_dir: Path, ref: str) -> Path | None:
    """在 Reports/summary 下查找文件名包含 ref 且以 .summary.md 结尾的文件。"""
    if not summary_dir.is_dir() or not ref:
        return None
    ref_upper = ref.upper()
    for path in summary_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if ref_upper in name.upper() and name.endswith(".summary.md"):
            return path
    return None


def _find_pdf_file(
    downloads_dir: Path,
    ref: str,
    *,
    subdir: str | None = None,
) -> Path | None:
    """
    在 JACAR_Downloads（可选子目录）下深度遍历，匹配文件名包含 ref 的 PDF。

    subdir 非空时仅在 downloads_dir/subdir 内查找（用于限定专题文件夹）。
    """
    if not downloads_dir.is_dir() or not ref:
        return None
    search_root = downloads_dir
    if subdir:
        search_root = downloads_dir / subdir
        if not search_root.is_dir():
            return None
    ref_upper = ref.upper()
    matches: list[Path] = []
    for root, _, files in os.walk(search_root):
        for name in files:
            if not name.lower().endswith(".pdf"):
                continue
            if ref_upper in name.upper():
                matches.append(Path(root) / name)
    if not matches:
        return None
    # 多条命中时取路径最短者（通常更贴近根目录规范命名）
    matches.sort(key=lambda p: (len(str(p)), str(p)))
    return matches[0]


def _read_summary(summary_path: Path | None) -> str:
    if summary_path is None or not summary_path.is_file():
        return ""
    try:
        return summary_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("读取宏观摘要失败: %s (%s)", summary_path, exc)
        return ""


def _page_render_zoom(
    page: Any,
    *,
    preview_dpi: float,
    max_pixels: int,
    jpeg_zoom: float | None,
) -> float:
    """按目标 DPI 计算 PyMuPDF 矩阵缩放；可选 jpeg_zoom 覆盖。"""
    if jpeg_zoom is not None:
        return max(0.5, float(jpeg_zoom))
    dpi = max(72.0, min(600.0, float(preview_dpi)))
    zoom = dpi / _PDF_BASE_DPI
    rect = page.rect
    longest_pt = max(float(rect.width), float(rect.height))
    cap = max(512, int(max_pixels))
    if longest_pt * zoom > cap:
        zoom = cap / longest_pt
    return max(0.5, zoom)


def _render_pdf_preview_images(
    pdf_path: Path,
    *,
    assets_dir: Path,
    ref: str,
    max_pages: int,
    preview_dpi: float = DEFAULT_PREVIEW_DPI,
    max_preview_pixels: int = DEFAULT_PREVIEW_MAX_PIXELS,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    jpeg_zoom: float | None = None,
) -> list[str]:
    """
    用 PyMuPDF 将 PDF 渲染为 JPEG，返回相对路径列表（供 HTML 引用）。
    相对路径形如 ./assets/<ref>/page_1.jpg（相对于 Reports/ 下 HTML）。
    max_pages<=0 时渲染全部页；否则最多 max_pages 页。
    默认约 240 DPI（与 DOCX 对照导出一致），最长边不超过 max_preview_pixels。
    """
    rel_paths: list[str] = []
    safe_ref = _safe_ref_dirname(ref)
    out_dir = assets_dir / safe_ref
    rel_prefix = f"./assets/{safe_ref}"
    quality = max(50, min(100, int(jpeg_quality)))

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        logger.warning("未安装 PyMuPDF (fitz)，跳过 PDF 预览图渲染 ref=%s: %s", ref, exc)
        return rel_paths

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with fitz.open(str(pdf_path)) as doc:
            total = _resolve_preview_page_cap(max_pages, len(doc))
            for i in range(total):
                page = doc[i]
                zoom = _page_render_zoom(
                    page,
                    preview_dpi=preview_dpi,
                    max_pixels=max_preview_pixels,
                    jpeg_zoom=jpeg_zoom,
                )
                matrix = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                filename = f"page_{i + 1}.jpg"
                out_file = out_dir / filename
                pix.save(str(out_file), jpg_quality=quality)
                rel_paths.append(f"{rel_prefix}/{filename}")
    except OSError as exc:
        logger.warning("写入预览图失败 ref=%s dir=%s: %s", ref, out_dir, exc)
    except Exception as exc:
        logger.warning("PDF 渲染失败 ref=%s path=%s: %s", ref, pdf_path, exc)

    return rel_paths


def _read_ocr_pages(ocr_cache_path: Path | None) -> list[str]:
    """读取 OCR 缓存 txt，解析 paged_v1，并按页提取 <transcription> 内日文。"""
    if ocr_cache_path is None or not ocr_cache_path.is_file():
        return []
    try:
        raw = ocr_cache_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取 OCR 缓存失败: %s (%s)", ocr_cache_path, exc)
        return []

    cache_service = CacheService()
    page_raw_list = cache_service.parse_paged_text(raw)
    return [extract_transcription_text(page) for page in page_raw_list]


def _list_analysis_json_files(json_dir: Path, ref: str) -> list[tuple[int, Path]]:
    """收集 Database_JSON/JACAR_{ref}_pXXXX.json 并按页码排序。"""
    if not json_dir.is_dir() or not ref:
        return []
    prefix = f"JACAR_{ref}_p"
    found: list[tuple[int, Path]] = []
    for path in json_dir.iterdir():
        if not path.is_file() or not path.name.lower().endswith(".json"):
            continue
        name = path.name
        if not name.upper().startswith(prefix.upper()):
            continue
        m = _JSON_PAGE_RE.search(name)
        if not m:
            continue
        found.append((int(m.group(1)), path))
    found.sort(key=lambda x: x[0])
    return found


def _load_analysis_fields(json_path: Path) -> dict[str, Any]:
    """从单页 Analysis JSON 提取抽屉/汇报常用字段。"""
    empty = {
        "date_written": "",
        "document_type": "",
        "observation_info": "",
        "core_judgment": "",
        "response_action": "",
        "relevance_score": "",
    }
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("解析 Analysis JSON 失败: %s (%s)", json_path, exc)
        return empty

    if not isinstance(payload, dict):
        return empty

    ctx = payload.get("Historical_Context") or {}
    disc = payload.get("Discourse_Analysis") or {}
    if not isinstance(ctx, dict):
        ctx = {}
    if not isinstance(disc, dict):
        disc = {}

    return {
        "date_written": str(ctx.get("Date_Written") or ""),
        "document_type": str(ctx.get("Document_Type") or ""),
        "observation_info": str(disc.get("Observation_Info") or ""),
        "core_judgment": str(disc.get("Core_Judgment") or ""),
        "response_action": str(disc.get("Response_Action") or ""),
        "relevance_score": str(disc.get("Relevance_Score") or ""),
    }


def _enrich_single_document(
    item: dict[str, Any],
    *,
    base_dir: Path,
    assets_dir: Path,
    max_preview_pages: int,
    preview_dpi: float,
    max_preview_pixels: int,
    jpeg_quality: int,
    jpeg_zoom: float | None,
    enrich_downloads_subdir: str,
) -> None:
    """为单条史料拼装 details；任何子步骤失败均降级为空值，不抛出到上层。"""
    ref = str(item.get("ref") or "").strip()
    details = _empty_details()
    item["details"] = details

    if not ref:
        logger.warning("跳过无 ref 的记录: %s", item)
        return

    summary_dir = base_dir / "Reports" / "summary"
    downloads_dir = base_dir / "JACAR_Downloads"
    ocr_dir = base_dir / "OCR_Cache"
    json_dir = base_dir / "Database_JSON"
    pdf_subdir = (enrich_downloads_subdir or "").strip() or None

    # 步骤 1：宏观摘要（Markdown 原文 + 预渲染 HTML）
    try:
        summary_path = _find_summary_file(summary_dir, ref)
        summary_text = _read_summary(summary_path)
        details["summary"] = summary_text
        details["summary_html"] = _markdown_to_html(summary_text)
        if summary_path is None:
            logger.warning("未找到宏观摘要 ref=%s (目录 %s)", ref, summary_dir)
    except Exception as exc:
        logger.warning("步骤1 宏观摘要异常 ref=%s: %s", ref, exc)
        details["summary"] = ""
        details["summary_html"] = ""

    # 步骤 2：PDF 原件 → 预览图
    pdf_path: Path | None = None
    image_rel_paths: list[str] = []
    try:
        pdf_path = _find_pdf_file(downloads_dir, ref, subdir=pdf_subdir)
        if pdf_path is None:
            logger.warning(
                "未找到 PDF 原件 ref=%s (目录 %s)",
                ref,
                downloads_dir / pdf_subdir if pdf_subdir else downloads_dir,
            )
        else:
            details["pdf_path"] = str(pdf_path.resolve())
            image_rel_paths = _render_pdf_preview_images(
                pdf_path,
                assets_dir=assets_dir,
                ref=ref,
                max_pages=max_preview_pages,
                preview_dpi=preview_dpi,
                max_preview_pixels=max_preview_pixels,
                jpeg_quality=jpeg_quality,
                jpeg_zoom=jpeg_zoom,
            )
    except Exception as exc:
        logger.warning("步骤2 PDF/预览图异常 ref=%s: %s", ref, exc)
        details["pdf_path"] = details.get("pdf_path") or ""

    # 步骤 3：OCR 缓存
    ocr_pages: list[str] = []
    try:
        if pdf_path and pdf_path.is_file():
            cache_file = Path(build_cache_path(str(pdf_path.resolve()), str(ocr_dir.resolve())))
            details["ocr_cache_path"] = str(cache_file)
            ocr_pages = _read_ocr_pages(cache_file)
            if not ocr_pages:
                logger.warning("OCR 缓存为空或未命中 ref=%s path=%s", ref, cache_file)
        else:
            logger.warning("跳过 OCR 读取（无 PDF）ref=%s", ref)
    except Exception as exc:
        logger.warning("步骤3 OCR 缓存异常 ref=%s: %s", ref, exc)
        details["ocr_cache_path"] = details.get("ocr_cache_path") or ""

    # 步骤 4：按页 JSON + 图片 + OCR 组装 pages_data
    pages_data: list[dict[str, Any]] = []
    try:
        json_entries = _list_analysis_json_files(json_dir, ref)
        if not json_entries:
            logger.warning("未找到 Database_JSON 分页文件 ref=%s (目录 %s)", ref, json_dir)

        for page_no, json_path in json_entries:
            fields = _load_analysis_fields(json_path)
            ocr_text = ""
            if ocr_pages and 1 <= page_no <= len(ocr_pages):
                ocr_text = ocr_pages[page_no - 1]
            image_path = ""
            if image_rel_paths and 1 <= page_no <= len(image_rel_paths):
                image_path = image_rel_paths[page_no - 1]

            pages_data.append(
                {
                    "page": page_no,
                    "image": image_path,
                    "ocr_text": ocr_text,
                    "date_written": fields["date_written"],
                    "document_type": fields["document_type"],
                    "observation_info": fields["observation_info"],
                    "core_judgment": fields["core_judgment"],
                    "response_action": fields["response_action"],
                    "relevance_score": fields["relevance_score"],
                }
            )

        # 若无 JSON 但有预览图/OCR，仍按预览页数生成占位页
        if not pages_data and (image_rel_paths or ocr_pages):
            page_count = max(len(image_rel_paths), len(ocr_pages))
            for idx in range(page_count):
                page_no = idx + 1
                pages_data.append(
                    {
                        "page": page_no,
                        "image": image_rel_paths[idx] if idx < len(image_rel_paths) else "",
                        "ocr_text": ocr_pages[idx] if idx < len(ocr_pages) else "",
                        "date_written": "",
                        "document_type": "",
                        "observation_info": "",
                        "core_judgment": "",
                        "response_action": "",
                        "relevance_score": "",
                    }
                )
    except Exception as exc:
        logger.warning("步骤4 分页数据组装异常 ref=%s: %s", ref, exc)
        pages_data = pages_data or []

    details["pages_data"] = pages_data


def enrich_document_details(
    data_list: list[dict[str, Any]],
    base_dir: str | Path,
    *,
    assets_dir: str | Path | None = None,
    enrich_cat: str = DEFAULT_ENRICH_CAT,
    enrich_downloads_subdir: str = DEFAULT_ENRICH_DOWNLOADS_SUBDIR,
    max_preview_pages: int = DEFAULT_MAX_PREVIEW_PAGES,
    preview_dpi: float = DEFAULT_PREVIEW_DPI,
    max_preview_pixels: int = DEFAULT_PREVIEW_MAX_PIXELS,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    jpeg_zoom: float | None = None,
) -> list[dict[str, Any]]:
    """
    遍历 data_list，按 ref 从磁盘拼图 summary / PDF 预览 / OCR / Analysis JSON。

    参数:
        data_list: fetch_documents 返回的字典列表（会被原地写入 details）。
        base_dir: 项目根目录（含 JACAR_Downloads、Database_JSON 等）。
        assets_dir: 预览图输出根目录，默认 base_dir/Reports/assets。
        enrich_cat: 仅对该 cat（话语主题）富化；其余条目不写入 details。
        enrich_downloads_subdir: 仅在 JACAR_Downloads/<subdir> 下查找 PDF。
        max_preview_pages: 每份 PDF 最多渲染页数；<=0 表示全页（默认 0）。
        preview_dpi: 预览图目标 DPI（默认 240，与 DOCX 对照导出一致）。
        max_preview_pixels: 单页最长边像素上限（默认 4800，防止超大图）。
        jpeg_quality: JPEG 质量 50–100（默认 92）。
        jpeg_zoom: 若指定则覆盖 preview_dpi 的缩放计算。

    返回:
        同一列表（便于链式调用）；富化专题条目含 details 键。
    """
    root = Path(base_dir).expanduser().resolve()
    assets_root = Path(assets_dir).expanduser().resolve() if assets_dir else root / "Reports" / "assets"
    assets_root.mkdir(parents=True, exist_ok=True)

    scope_label = str(enrich_cat or "").strip()
    subdir_label = str(enrich_downloads_subdir or "").strip()
    in_scope = [it for it in data_list if _item_in_enrich_scope(it, enrich_cat)]
    logger.info(
        "富化范围: cat=%s, PDF 子目录=JACAR_Downloads/%s, 命中 %d/%d 条",
        scope_label,
        subdir_label,
        len(in_scope),
        len(data_list),
    )

    total = len(data_list)
    for idx, item in enumerate(data_list, start=1):
        ref = item.get("ref", "")
        if not _item_in_enrich_scope(item, enrich_cat):
            item.pop("details", None)
            continue

        logger.info("富化史料 [%d/%d] ref=%s cat=%s", idx, total, ref, item.get("cat", ""))
        try:
            _enrich_single_document(
                item,
                base_dir=root,
                assets_dir=assets_root,
                max_preview_pages=max_preview_pages,
                preview_dpi=preview_dpi,
                max_preview_pixels=max_preview_pixels,
                jpeg_quality=jpeg_quality,
                jpeg_zoom=jpeg_zoom,
                enrich_downloads_subdir=subdir_label,
            )
        except Exception as exc:
            # 最后一道防线：单条彻底失败也不中断批处理
            logger.warning("富化单条记录失败 ref=%s: %s", ref, exc)
            item["details"] = _empty_details()

    return data_list


def _find_matching_array_end(html: str, open_bracket_index: int) -> int:
    """从 rawData 起始 `[` 起做括号匹配，返回与之对应的 `]` 下标。"""
    if open_bracket_index >= len(html) or html[open_bracket_index] != "[":
        raise ValueError("open_bracket_index 必须指向 `[`")

    depth = 0
    in_str = False
    esc = False
    for i in range(open_bracket_index, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
    raise RuntimeError("HTML 中 rawData 数组未闭合（缺少 `]`）")


def _find_raw_data_span(html: str) -> tuple[int, int]:
    """
    定位 `const rawData = [...];` 在 HTML 中的替换区间 [start, end)。

    不用非贪婪正则 `.*?];`，避免 OCR/摘要字符串里出现 `];` 时被截断。
    """
    marker = _RAW_DATA_MARKER.search(html)
    if not marker:
        raise RuntimeError(
            "未在 HTML 中找到 `const rawData = [`，请确认模板格式与 HRS_Database_Demo2.html 一致。"
        )

    start = marker.start()
    bracket_start = html.index("[", marker.end() - 1)
    bracket_end = _find_matching_array_end(html, bracket_start)
    end = bracket_end + 1
    while end < len(html) and html[end] in " \t\r\n":
        end += 1
    if end < len(html) and html[end] == ";":
        end += 1
    return start, end


def _serialize_raw_data(data: list[dict[str, Any]]) -> str:
    """序列化为可嵌入 <script> 的 JSON 文本，并校验可被 json.loads 解析。"""
    blob = json.dumps(data, ensure_ascii=False, indent=4)
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"rawData JSON 序列化后无法解析: {exc}") from exc
    if len(parsed) != len(data):
        raise RuntimeError("rawData JSON 条数与源数据不一致")
    return blob


def _validate_embedded_raw_data(html: str) -> int:
    """注入后校验 rawData 块为合法 JSON 数组，返回记录条数。"""
    marker = _RAW_DATA_MARKER.search(html)
    if not marker:
        raise RuntimeError("注入后的 HTML 缺少 rawData 声明")
    bracket_start = html.index("[", marker.end() - 1)
    bracket_end = _find_matching_array_end(html, bracket_start)
    blob = html[bracket_start : bracket_end + 1]
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "注入后的 rawData 不是合法 JSON（字符串中不能有未转义换行）。"
            f" 详情: {exc}"
        ) from exc
    if not isinstance(parsed, list):
        raise RuntimeError("rawData 必须是 JSON 数组")
    return len(parsed)


def inject_to_html(
    data: list[dict[str, Any]],
    *,
    html_path: Path,
    output_path: Path | None = None,
) -> Path:
    if not html_path.is_file():
        raise FileNotFoundError(f"HTML 模板不存在: {html_path}")

    json_data = _serialize_raw_data(data)
    replacement = f"const rawData = {json_data};"

    html_content = html_path.read_text(encoding="utf-8")
    span = _find_raw_data_span(html_content)
    new_html_content = html_content[: span[0]] + replacement + html_content[span[1] :]

    record_count = _validate_embedded_raw_data(new_html_content)
    if record_count != len(data):
        raise RuntimeError(
            f"注入校验失败：HTML 内 rawData 条数 {record_count} 与数据库 {len(data)} 不一致"
        )

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = html_path.with_name(f"HRS_Database_Report_{ts}.html")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(new_html_content, encoding="utf-8")
    return output_path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="将 SQLite documents 注入汇报 HTML 演示页")
    parser.add_argument("--db-path", default=None, help="SQLite 路径（默认 database/hrs.sqlite3）")
    parser.add_argument(
        "--html-path",
        default=None,
        help="源 HTML 模板路径（默认见 --release）",
    )
    parser.add_argument("--output", default=None, help="输出 HTML 路径（默认见 --release）")
    parser.add_argument(
        "--release",
        action="store_true",
        help="发布模式：Demo2 模板 + 富化数据 + 输出 Reports/HRS_Final_Release.html",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="富化 summary / PDF 预览图 / OCR / Database_JSON 分页数据并写入 details",
    )
    parser.add_argument(
        "--base-dir",
        default=str(_PROJECT_ROOT),
        help="项目根目录（默认脚本上级目录）",
    )
    parser.add_argument(
        "--assets-dir",
        default=None,
        help="预览图输出目录（默认 <base-dir>/Reports/assets）",
    )
    parser.add_argument(
        "--enrich-cat",
        default=DEFAULT_ENRICH_CAT,
        help=f"仅对该话语主题富化 PDF/OCR/Analysis/summary（默认 {DEFAULT_ENRICH_CAT!r}）",
    )
    parser.add_argument(
        "--enrich-downloads-subdir",
        default=DEFAULT_ENRICH_DOWNLOADS_SUBDIR,
        help=(
            "仅在 JACAR_Downloads 下该子目录查找 PDF "
            f"（默认 {DEFAULT_ENRICH_DOWNLOADS_SUBDIR!r}）"
        ),
    )
    parser.add_argument(
        "--max-preview-pages",
        type=int,
        default=DEFAULT_MAX_PREVIEW_PAGES,
        help="每份 PDF 最多渲染预览页数；0 表示不限制（渲染全部页，默认 0）",
    )
    parser.add_argument(
        "--preview-dpi",
        type=float,
        default=DEFAULT_PREVIEW_DPI,
        help="预览图目标 DPI（默认 240；旧版约 144 对应 --jpeg-zoom 2）",
    )
    parser.add_argument(
        "--max-preview-pixels",
        type=int,
        default=DEFAULT_PREVIEW_MAX_PIXELS,
        help="单页预览图最长边像素上限（默认 4800）",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=DEFAULT_JPEG_QUALITY,
        help="JPEG 保存质量 50–100（默认 92）",
    )
    parser.add_argument(
        "--jpeg-zoom",
        type=float,
        default=None,
        help="可选：直接指定 PyMuPDF 矩阵缩放，覆盖 --preview-dpi",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db_path)
    base_dir = Path(args.base_dir).expanduser().resolve()

    do_release = bool(args.release)
    do_enrich = bool(args.enrich) or do_release

    if args.html_path:
        html_path = Path(args.html_path).expanduser().resolve()
    elif do_release:
        html_path = DEFAULT_HTML_DRAWER
    else:
        html_path = DEFAULT_HTML

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
    elif do_release:
        output_path = DEFAULT_RELEASE_HTML
    else:
        output_path = None

    data = fetch_documents(db_path)

    if do_enrich:
        try:
            import markdown  # noqa: F401
        except ImportError:
            logger.warning(
                "未安装 Python 包 markdown，summary_html 将回退为纯文本；"
                "发布页仍可用 marked.js 渲染 details.summary。建议: pip install markdown"
            )

        enrich_document_details(
            data,
            base_dir,
            assets_dir=args.assets_dir,
            enrich_cat=str(args.enrich_cat),
            enrich_downloads_subdir=str(args.enrich_downloads_subdir),
            max_preview_pages=int(args.max_preview_pages),
            preview_dpi=float(args.preview_dpi),
            max_preview_pixels=max(512, int(args.max_preview_pixels)),
            jpeg_quality=int(args.jpeg_quality),
            jpeg_zoom=float(args.jpeg_zoom) if args.jpeg_zoom is not None else None,
        )
        scope_n = sum(1 for d in data if _item_in_enrich_scope(d, str(args.enrich_cat)))
        enriched_count = sum(1 for d in data if (d.get("details") or {}).get("pages_data"))
        logger.info(
            "富化完成：专题 %d 条，其中 %d 条含 pages_data；其余 %d 条仅目录条目",
            scope_n,
            enriched_count,
            len(data) - scope_n,
        )

    out = inject_to_html(data, html_path=html_path, output_path=output_path)

    print(f"数据库: {db_path}")
    print(f"模板:   {html_path}")
    print(f"成功注入 {len(data)} 条记录")
    if do_enrich:
        dpi = float(args.preview_dpi)
        pages_hint = (
            "全部页"
            if int(args.max_preview_pages) <= 0
            else f"最多 {int(args.max_preview_pages)} 页"
        )
        print(
            f"富化:   已启用（专题 {args.enrich_cat!r}，PDF 目录 JACAR_Downloads/{args.enrich_downloads_subdir}，"
            f"预览 {pages_hint}、约 {dpi:.0f} DPI、JPEG {int(args.jpeg_quality)}）"
        )
    print(f"已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
