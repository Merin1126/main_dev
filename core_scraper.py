# core_scraper.py
import time
import sys
import os
import shutil
import traceback
import re
import json
import logging
from urllib.parse import urlencode, urlparse
import threading
import queue
import requests
import fitz
from html import unescape
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import parse_qs

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from services import DbService

_PRINT_LOCK = threading.Lock()
_STATUS_LINE_LEN = 0

_DOWNLOAD_EVENT_CODE_TEXT = {
    "E_JACAR_CONTENT_LIST_MISSING": "JACAR 页面缺少 najContentList",
    "E_JACAR_CONTENT_LIST_PARSE": "JACAR najContentList 解析失败",
    "E_JACAR_CONTENT_LIST_EMPTY": "JACAR najContentList 为空",
    "E_JACAR_REL_PATH_MISSING": "JACAR 首条缺少 path/source",
    "E_TOYO_OPAC_RESOLVE_FAILED": "OPAC 跳板解析失败",
    "E_TOYO_MANIFEST_MISSING": "Toyo Bunko manifest 缺失",
    "E_TOYO_CANVASES_EMPTY": "Toyo Bunko canvases 为空",
    "E_TOYO_IIIF_INCOMPLETE": "IIIF 组装未完成",
    "E_TOYO_IIIF_ZERO_PAGES": "IIIF 未提取到有效页",
    "E_UNSUPPORTED_DOMAIN": "未支持的来源域名",
    "E_TASK_EXCEPTION": "任务处理异常",
    "A_MANUAL_STOP_DIRECT": "手动停止：直链下载中止",
    "A_MANUAL_STOP_IIIF": "手动停止：IIIF 组装中止",
}


def _event_message(code: str, detail: str | None = None) -> str:
    """
    统一事件消息格式：CODE | 中文说明 | detail。
    便于 GUI 与后续脚本按 code 聚合。
    """
    text = _DOWNLOAD_EVENT_CODE_TEXT.get(code, "未知事件")
    suffix = f" | detail={detail}" if detail else ""
    return f"{code} | {text}{suffix}"


def _clear_status_line_locked():
    """清理当前终端状态行（需在 _PRINT_LOCK 内调用）。"""
    global _STATUS_LINE_LEN
    if _STATUS_LINE_LEN > 0:
        print("\r" + (" " * _STATUS_LINE_LEN) + "\r", end="", flush=True)
        _STATUS_LINE_LEN = 0


def _render_status_line_locked(line: str):
    """渲染单行状态（需在 _PRINT_LOCK 内调用）。"""
    global _STATUS_LINE_LEN
    # 先清空整行再写入，避免中英文宽度差导致的残留字符
    print("\r\033[2K" + line, end="", flush=True)
    _STATUS_LINE_LEN = len(line)


def _build_run_logger(log_root: str, keyword: str) -> tuple[logging.Logger, str]:
    os.makedirs(log_root, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_kw = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", keyword).strip("_") or "kw"
    log_path = os.path.join(log_root, f"scraper_run_{safe_kw}_{ts}.log")
    logger_name = f"core_scraper_run_{ts}_{os.getpid()}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    return logger, log_path


def _emit(logger: logging.Logger | None, message: str, level: int = logging.INFO):
    """统一日志出口：控制台打印 +（可选）文件日志。"""
    with _PRINT_LOCK:
        _clear_status_line_locked()
        print(message, flush=True)
    if logger is not None:
        logger.log(level, message)


def _safe_callback(callback, *args, **kwargs):
    """安全触发回调，避免 UI 回调异常影响主流程。"""
    if callback is None:
        return
    try:
        callback(*args, **kwargs)
    except Exception:
        pass


def _format_bytes(num_bytes: float | int | None) -> str:
    """人类可读的字节格式。"""
    if num_bytes is None:
        return "未知"
    n = float(max(0, num_bytes))
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while n >= 1024 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    return f"{n:.2f}{units[idx]}"


def _progress_bar(current: int, total: int | None, width: int = 24) -> str:
    if not total or total <= 0:
        # 未知总大小：给一个动态“跑马灯”效果
        step = (int(time.time() * 5) % width)
        chars = ["-"] * width
        chars[step] = ">"
        return "".join(chars)
    ratio = max(0.0, min(1.0, current / total))
    filled = int(width * ratio)
    return "#" * filled + "-" * (width - filled)


def _stream_with_progress(
    response,
    write_chunk,
    label: str,
    chunk_size: int = 1024 * 64,
    refresh_interval: float = 0.35,
    show_done_line: bool = True,
    finalize_line_break: bool = True,
    show_live_line: bool = True,
    should_stop=None,
    on_progress=None,
) -> tuple[int, int | None, bool]:
    """
    流式写入并在终端显示模拟进度条（类似 pip）。
    write_chunk: Callable[[bytes], None]
    """
    global _STATUS_LINE_LEN
    total = None
    content_length = response.headers.get("Content-Length")
    if content_length and str(content_length).isdigit():
        total = int(content_length)

    downloaded = 0
    start_ts = time.time()
    last_render_ts = 0.0
    short_label = label if len(label) <= 34 else (label[:31] + "...")
    aborted = False

    for chunk in response.iter_content(chunk_size=chunk_size):
        if callable(should_stop) and should_stop():
            aborted = True
            break
        if not chunk:
            continue
        write_chunk(chunk)
        downloaded += len(chunk)
        now = time.time()
        if show_live_line and (now - last_render_ts >= max(0.1, float(refresh_interval))):
            elapsed = max(now - start_ts, 1e-6)
            speed = downloaded / elapsed
            bar = _progress_bar(downloaded, total, width=24)
            pct = f"{(downloaded / total) * 100:6.2f}%" if total else "  N/A "
            line = (
                f"  -> ⏬ [{short_label}] [{bar}] {pct} | "
                f"{_format_bytes(downloaded)} / {_format_bytes(total)} | {_format_bytes(speed)}/s"
            )
            with _PRINT_LOCK:
                _render_status_line_locked(line)
            _safe_callback(
                on_progress,
                downloaded=downloaded,
                total=total,
                speed_bps=speed,
            )
            last_render_ts = now

    elapsed = max(time.time() - start_ts, 1e-6)
    speed = downloaded / elapsed
    final_bar = _progress_bar(downloaded, total or downloaded or 1, width=24)
    final_pct = f"{(downloaded / total) * 100:6.2f}%" if total else "100.00%"
    done_line = (
        f"  -> ✅ [{short_label}] [{final_bar}] {final_pct} | "
        f"{_format_bytes(downloaded)} / {_format_bytes(total if total else downloaded)} | {_format_bytes(speed)}/s"
    )
    with _PRINT_LOCK:
        if show_done_line and not aborted:
            _render_status_line_locked(done_line)
        if finalize_line_break:
            print("", flush=True)
            _STATUS_LINE_LEN = 0
    _safe_callback(
        on_progress,
        downloaded=downloaded,
        total=total,
        speed_bps=speed,
    )

    return downloaded, total, aborted


def _render_iiif_batch_progress(
    done_pages: int,
    total_pages: int,
    bytes_downloaded: int,
    bytes_total_known: int,
    current_page: int,
    start_ts: float | None = None,
):
    """IIIF 组装的卷级进度显示（单行刷新）。"""
    bar = _progress_bar(done_pages, total_pages if total_pages > 0 else 1, width=20)
    pct = f"{(done_pages / total_pages) * 100:6.2f}%" if total_pages > 0 else "  0.00%"
    total_text = _format_bytes(bytes_total_known) if bytes_total_known > 0 else "未知"
    if start_ts is None:
        start_ts = time.time()
    elapsed = max(time.time() - start_ts, 1e-6)
    speed = bytes_downloaded / elapsed
    line = (
        f"  -> 🧩 IIIF [{bar}] {pct} | 页 {done_pages}/{total_pages} | "
        f"体积 {_format_bytes(bytes_downloaded)}/{total_text} | "
        f"速率 {_format_bytes(speed)}/s | 当前页 p{current_page:03d}"
    )
    with _PRINT_LOCK:
        _render_status_line_locked(line)


def _render_global_progress(progress_state: dict):
    """
    渲染全局任务进度（任务数 + 总下载量 + 平均速度）。
    使用普通换行，避免与多线程文件进度条互相覆盖。
    """
    lock = progress_state.get("lock")
    if lock is None:
        return
    with lock:
        dispatched = int(progress_state.get("dispatched", 0))
        completed = int(progress_state.get("completed", 0))
        succeeded = int(progress_state.get("succeeded", 0))
        failed = int(progress_state.get("failed", 0))
        bytes_downloaded = int(progress_state.get("bytes_downloaded", 0))
        bytes_total_known = int(progress_state.get("bytes_total_known", 0))
        start_ts = float(progress_state.get("start_ts", time.time()))

    bar = _progress_bar(completed, dispatched if dispatched > 0 else 1, width=20)
    pct = f"{(completed / dispatched) * 100:6.2f}%" if dispatched > 0 else "  0.00%"
    elapsed = max(time.time() - start_ts, 1e-6)
    avg_speed = bytes_downloaded / elapsed
    total_text = _format_bytes(bytes_total_known) if bytes_total_known > 0 else "未知"
    line = (
        f"  -> 🌐 总进度 [{bar}] {pct} | 完成 {completed}/{dispatched} | "
        f"成功 {succeeded} 失败 {failed} | 总量 {_format_bytes(bytes_downloaded)}/{total_text} | "
        f"均速 {_format_bytes(avg_speed)}/s"
    )
    with _PRINT_LOCK:
        _render_status_line_locked(line)


def _append_jsonl(path: str, payload: dict):
    """安全追加一条 JSONL 记录。"""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # 不让失败证据写入反过来影响主抓取流程
        pass


def _write_sidecar_metadata(
    save_path: str,
    metadata: dict,
    logger: logging.Logger | None = None,
) -> str:
    """为 PDF 写入同名 Sidecar JSON，返回 json 路径。"""
    json_path = os.path.splitext(save_path)[0] + ".json"
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(metadata or {}, jf, ensure_ascii=False, indent=4)
    _emit(logger, f"  -> 🧾 Sidecar 元数据已保存: {os.path.basename(json_path)}")
    return json_path


def _wait_dom_ready(driver, timeout=25):
    """等待页面 DOM 完整就绪，减少首屏元素偶发找不到的问题。"""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def _sanitize_filename(name: str) -> str:
    """替换 Windows/macOS 非法字符，避免保存失败。"""
    trans = {
        "\\": "＼",
        "/": "／",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "”",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
    return re.sub(r'[\\/:*?"<>|]', lambda m: trans[m.group()], name).strip(" .")


def _extract_first_digits(text: str, default: str = "1") -> str:
    if not text:
        return default
    m = re.search(r"\d+", str(text))
    return m.group(0) if m else default


def _iiif_resume_dir(save_path: str) -> str:
    """IIIF 页级断点目录。"""
    return os.path.splitext(save_path)[0] + ".iiif_resume"


def _iiif_part_pdf_path(resume_dir: str, page_num: int) -> str:
    """IIIF 单页缓存文件路径。"""
    return os.path.join(resume_dir, f"page_{page_num:04d}.pdf")


def _hoover_pending_has_url(pending_path: str, viewer_url: str) -> bool:
    """检查 Hoover 待处理清单中是否已存在同一链接。"""
    if not pending_path or not viewer_url or not os.path.exists(pending_path):
        return False
    try:
        with open(pending_path, "r", encoding="utf-8") as f:
            for line in f:
                if viewer_url in line:
                    return True
    except Exception:
        return False
    return False


def _extract_toyobunko_digital_url(page_html: str) -> str | None:
    """从 OPAC HTML 中提取 Digital Version 对应的 app 阅读器地址。"""
    if not page_html:
        return None
    html_text = unescape(page_html)
    m = re.search(
        r'href\s*=\s*["\'](https://app\.toyobunko-lab\.jp/s/main/document/[^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _resolve_opac_toyobunko_url(
    session: requests.Session,
    headers: dict,
    viewer_url: str,
    logger: logging.Logger | None = None,
) -> str | None:
    """
    解析 OPAC 跳板页，提取真正的 app.toyobunko-lab.jp 阅读器地址。
    优先尝试稳定的 detail?bbid= 永久链接，规避 search/detail 会话超时问题。
    """
    parsed = urlparse(viewer_url)
    query = parse_qs(parsed.query or "")
    bbid = (query.get("bbid") or query.get("bibId") or [None])[0]

    candidates: list[str] = []
    if bbid:
        candidates.append(f"https://opac.tbopac.com/detail?bbid={bbid}")
    candidates.append(viewer_url)

    # 去重保序
    seen: set[str] = set()
    uniq_candidates: list[str] = []
    for u in candidates:
        if u and u not in seen:
            uniq_candidates.append(u)
            seen.add(u)

    for idx, url in enumerate(uniq_candidates, start=1):
        try:
            _emit(logger, f"  -> 🔎 OPAC 跳板解析尝试 {idx}/{len(uniq_candidates)}: {url}")
            resp = session.get(url, headers=headers, timeout=20)
            page = resp.text or ""
            digital_url = _extract_toyobunko_digital_url(page)
            if digital_url:
                return digital_url

            # 若最终跳转到 search/detail 会话页，尝试二次规范化
            final_query = parse_qs(urlparse(resp.url).query or "")
            final_bbid = (final_query.get("bbid") or final_query.get("bibId") or [None])[0]
            if final_bbid:
                canonical = f"https://opac.tbopac.com/detail?bbid={final_bbid}"
                if canonical not in seen:
                    seen.add(canonical)
                    uniq_candidates.append(canonical)
        except Exception as e:
            _emit(logger, f"  -> ⚠️ OPAC 跳板页面请求失败（已继续尝试其他候选）: {e}", logging.WARNING)
            continue

    return None

# ==========================================
# 🐝 全新后台打工人：纯 API 文件拉取引擎
# ==========================================
def api_download_worker(
    task_queue,
    stop_event,
    logger: logging.Logger | None = None,
    progress_state: dict | None = None,
    on_task_update=None,
):
    """后台打工人：按域名分流下载策略（直链 / IIIF / 宕机登记）。"""
    db_service = DbService()
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        )
    }

    while not stop_event.is_set():
        try:
            task = task_queue.get(timeout=2)
        except queue.Empty:
            continue

        if task is None:
            task_queue.task_done()
            break

        viewer_url = task.get("url", "")
        save_path = task.get("save_path", "")
        doc_title = task.get("title", "未知文档")
        doc_metadata = task.get("metadata", {}) or {}
        task_mode = task.get("mode", "download_and_sidecar")
        task_id = task.get("task_id") or save_path
        source = str(task.get("source") or "jacar")
        native_id = str(task.get("native_id") or doc_metadata.get("Ref_Code") or "Unknown_Ref")
        run_id = task.get("run_id")
        task_success = False
        task_bytes_downloaded = 0
        task_bytes_total_known = 0

        def _mark_task_terminal_state(event_type: str, message: str = "", doc_status: str | None = None):
            """为非异常早退路径补齐状态闭环，避免任务长期停留在 downloading。"""
            if doc_status:
                try:
                    db_service.mark_document_status(source, native_id, doc_status)
                except Exception:
                    pass
            try:
                db_service.add_download_event(
                    native_id,
                    event_type,
                    message=message,
                    run_id=int(run_id) if run_id is not None else None,
                    source=source,
                )
            except Exception:
                pass

        try:
            _emit(logger, f"  -> 🐝 [打工人接单] {doc_title} | mode={task_mode}")
            _safe_callback(
                on_task_update,
                task_id,
                {"status": "正在下载", "progress": 0.0, "speed_text": "0.00B/s"},
            )
            db_service.add_download_event(
                native_id,
                "downloading",
                run_id=int(run_id) if run_id is not None else None,
                source=source,
            )

            if task_mode == "sidecar_only":
                _write_sidecar_metadata(save_path, doc_metadata, logger)
                db_service.mark_downloaded_with_files(
                    source=source,
                    native_id=native_id,
                    pdf_path=save_path if os.path.exists(save_path) else None,
                    sidecar_path=os.path.splitext(save_path)[0] + ".json",
                )
                db_service.add_download_event(
                    native_id,
                    "succeeded",
                    run_id=int(run_id) if run_id is not None else None,
                    source=source,
                )
                _emit(
                    logger,
                    f"  -> ✅ [打工人完工] 检测到 PDF 已存在，仅补写 Sidecar 元数据 JSON: {os.path.basename(save_path)}",
                )
                task_success = True
                _safe_callback(
                    on_task_update,
                    task_id,
                    {"status": "已下载", "progress": 1.0, "speed_text": "N/A"},
                )
                continue

            # 物理自愈兜底：数据库状态丢失但 PDF 已存在时，直接修复数据库并跳过网络下载
            if os.path.exists(save_path):
                sidecar_path = os.path.splitext(save_path)[0] + ".json"
                db_service.mark_downloaded_with_files(
                    source=source,
                    native_id=native_id,
                    pdf_path=save_path,
                    sidecar_path=sidecar_path if os.path.exists(sidecar_path) else None,
                )
                db_service.add_download_event(
                    native_id,
                    "succeeded",
                    message="物理文件已存在，已自动修复数据库状态",
                    run_id=int(run_id) if run_id is not None else None,
                    source=source,
                )
                _emit(logger, "  -> ♻️ 物理文件已存在，已自动修复数据库状态", logging.INFO)
                task_success = True
                _safe_callback(
                    on_task_update,
                    task_id,
                    {"status": "已下载", "progress": 1.0, "speed_text": "完成"},
                )
                continue

            # ------------------------------------------
            # 分支 A：JACAR / NAJ 常规直链战术
            # ------------------------------------------
            if "jacar.archives.go.jp" in viewer_url or "digital.archives.go.jp" in viewer_url:
                res = session.get(viewer_url, headers=headers, timeout=20)
                html = res.text
                m = re.search(r"var\s+najContentList\s*=\s*(\[.*?\]);", html, re.DOTALL)
                if not m:
                    _emit(logger, "  -> ⚠️ [打工人] 未找到 najContentList，跳过该条。", logging.WARNING)
                    _mark_task_terminal_state(
                        "failed",
                        _event_message("E_JACAR_CONTENT_LIST_MISSING"),
                        doc_status="failed",
                    )
                    continue
                try:
                    payload = json.loads(m.group(1))
                except Exception as e:
                    _emit(logger, f"  -> ⚠️ [打工人] najContentList JSON 解析失败: {e}", logging.WARNING)
                    _mark_task_terminal_state(
                        "failed",
                        _event_message("E_JACAR_CONTENT_LIST_PARSE", str(e)),
                        doc_status="failed",
                    )
                    continue

                if not isinstance(payload, list) or not payload:
                    _emit(logger, "  -> ⚠️ [打工人] najContentList 为空，跳过该条。", logging.WARNING)
                    _mark_task_terminal_state(
                        "failed",
                        _event_message("E_JACAR_CONTENT_LIST_EMPTY"),
                        doc_status="failed",
                    )
                    continue
                first = payload[0] if isinstance(payload[0], dict) else {}
                rel_path = first.get("path") or first.get("source")
                if not rel_path:
                    _emit(logger, "  -> ⚠️ [打工人] 首项缺少 path/source，跳过该条。", logging.WARNING)
                    _mark_task_terminal_state(
                        "failed",
                        _event_message("E_JACAR_REL_PATH_MISSING"),
                        doc_status="failed",
                    )
                    continue

                if str(rel_path).startswith(("http://", "https://")):
                    pdf_url = str(rel_path)
                else:
                    parsed = urlparse(viewer_url)
                    root = f"{parsed.scheme}://{parsed.netloc}"
                    rel_path = str(rel_path)
                    if not rel_path.startswith("/"):
                        rel_path = "/" + rel_path
                    pdf_url = root + rel_path

                with session.get(pdf_url, headers=headers, timeout=60, stream=True) as dl:
                    dl.raise_for_status()
                    with open(save_path, "wb") as f:
                        downloaded, total_known, aborted = _stream_with_progress(
                            dl,
                            f.write,
                            label=f"直链 {os.path.basename(save_path)}",
                            should_stop=stop_event.is_set,
                            on_progress=lambda downloaded, total, speed_bps: _safe_callback(
                                on_task_update,
                                task_id,
                                {
                                    "status": "正在下载",
                                    "progress": (downloaded / total) if total else None,
                                    "speed_text": f"{_format_bytes(speed_bps)}/s",
                                },
                            ),
                        )
                        task_bytes_downloaded += downloaded
                        task_bytes_total_known += (total_known or 0)
                        if aborted:
                            _emit(logger, f"  -> ⏹️ [打工人] 手动停止已触发，中止当前直链下载: {doc_title}")
                            _mark_task_terminal_state(
                                "aborted",
                                _event_message("A_MANUAL_STOP_DIRECT"),
                                doc_status="discovered",
                            )
                            _safe_callback(
                                on_task_update,
                                task_id,
                                {"status": "已中止", "progress": None, "speed_text": "0.00B/s"},
                            )
                            continue
                _write_sidecar_metadata(save_path, doc_metadata, logger)
                db_service.mark_downloaded_with_files(
                    source=source,
                    native_id=native_id,
                    pdf_path=save_path,
                    sidecar_path=os.path.splitext(save_path)[0] + ".json",
                )
                db_service.add_download_event(
                    native_id,
                    "succeeded",
                    run_id=int(run_id) if run_id is not None else None,
                    source=source,
                )
                _emit(logger, f"  -> ✅ [打工人完工] 直链下载成功，并已保存元数据 JSON: {os.path.basename(save_path)}")
                task_success = True
                _safe_callback(
                    on_task_update,
                    task_id,
                    {"status": "已下载", "progress": 1.0, "speed_text": "完成"},
                )
                continue

            # ------------------------------------------
            # 分支 B：東洋文庫 IIIF 组装战术
            # ------------------------------------------
            elif "toyobunko-lab.jp" in viewer_url or "opac.tbopac.com" in viewer_url:
                # OPAC 跳板页：先解析真实阅读器地址，再进入统一 IIIF 流程
                if "opac.tbopac.com" in viewer_url:
                    resolved_url = _resolve_opac_toyobunko_url(session, headers, viewer_url, logger)
                    if resolved_url:
                        viewer_url = resolved_url
                        _emit(logger, f"  -> ✅ 成功解析 OPAC 跳板地址: {viewer_url}")
                    else:
                        _emit(logger, "  -> ⚠️ 未能在 OPAC 页面找到 Digital Version 链接", logging.WARNING)
                        _mark_task_terminal_state(
                            "failed",
                            _event_message("E_TOYO_OPAC_RESOLVE_FAILED"),
                            doc_status="failed",
                        )
                        continue

                page = session.get(viewer_url, headers=headers, timeout=20).text
                mm = re.search(
                    r"manifest=(https://app\.toyobunko-lab\.jp/iiif/2/[^/]+/manifest)",
                    page,
                )
                if not mm:
                    _emit(logger, "  -> ⚠️ [打工人] 未抓到 Toyo Bunko manifest URL，跳过。", logging.WARNING)
                    _mark_task_terminal_state(
                        "failed",
                        _event_message("E_TOYO_MANIFEST_MISSING"),
                        doc_status="failed",
                    )
                    continue
                manifest_url = mm.group(1)
                manifest = session.get(manifest_url, headers=headers, timeout=20).json()

                canvases = (
                    manifest.get("sequences", [{}])[0].get("canvases", [])
                    if isinstance(manifest, dict)
                    else []
                )
                if not canvases:
                    _emit(logger, "  -> ⚠️ [打工人] manifest canvases 为空，跳过。", logging.WARNING)
                    _mark_task_terminal_state(
                        "failed",
                        _event_message("E_TOYO_CANVASES_EMPTY"),
                        doc_status="failed",
                    )
                    continue

                pdf_doc = fitz.open()
                img_count = 0
                total_pages = len(canvases)
                iiif_bytes_downloaded = 0
                iiif_bytes_total_known = 0
                iiif_resume_hits = 0
                aborted_iiif = False
                iiif_start_ts = time.time()
                resume_dir = _iiif_resume_dir(save_path)
                os.makedirs(resume_dir, exist_ok=True)
                try:
                    for page_num, canvas in enumerate(canvases, start=1):
                        if stop_event.is_set():
                            _emit(logger, f"  -> ⏹️ [打工人] 手动停止已触发，终止 IIIF 组装: {doc_title}")
                            aborted_iiif = True
                            break
                        part_pdf_path = _iiif_part_pdf_path(resume_dir, page_num)
                        if os.path.exists(part_pdf_path):
                            try:
                                part_doc = fitz.open(part_pdf_path)
                                try:
                                    pdf_doc.insert_pdf(part_doc)
                                finally:
                                    part_doc.close()
                                img_count += 1
                                iiif_resume_hits += 1
                                _render_iiif_batch_progress(
                                    done_pages=img_count,
                                    total_pages=total_pages,
                                    bytes_downloaded=iiif_bytes_downloaded,
                                    bytes_total_known=iiif_bytes_total_known,
                                    current_page=page_num,
                                    start_ts=iiif_start_ts,
                                )
                                continue
                            except Exception as reuse_e:
                                _emit(
                                    logger,
                                    f"  -> ⚠️ [打工人] IIIF 断点页损坏，将重下该页 p{page_num:03d}: {reuse_e}",
                                    logging.WARNING,
                                )
                                try:
                                    os.remove(part_pdf_path)
                                except Exception:
                                    pass
                        try:
                            img_url = canvas["images"][0]["resource"]["@id"]
                        except Exception:
                            continue
                        try:
                            with session.get(img_url, headers=headers, timeout=30, stream=True) as img_resp:
                                img_resp.raise_for_status()
                                img_buffer = bytearray()
                                downloaded, total_known, aborted = _stream_with_progress(
                                    img_resp,
                                    img_buffer.extend,
                                    label=f"IIIF p{page_num:03d}",
                                    refresh_interval=0.5,
                                    show_done_line=False,
                                    finalize_line_break=False,
                                    show_live_line=False,
                                    should_stop=stop_event.is_set,
                                )
                                task_bytes_downloaded += downloaded
                                task_bytes_total_known += (total_known or 0)
                                iiif_bytes_downloaded += downloaded
                                iiif_bytes_total_known += (total_known or 0)
                                if aborted:
                                    _emit(logger, f"  -> ⏹️ [打工人] 手动停止已触发，中止 IIIF 当前页下载: p{page_num:03d}")
                                    aborted_iiif = True
                                    break
                            if aborted:
                                break
                            img_bytes = bytes(img_buffer)
                            img_doc = fitz.open(stream=img_bytes, filetype="jpeg")
                            try:
                                img_pdf = fitz.open("pdf", img_doc.convert_to_pdf())
                                try:
                                    img_pdf.save(part_pdf_path)
                                    pdf_doc.insert_pdf(img_pdf)
                                finally:
                                    img_pdf.close()
                            finally:
                                img_doc.close()
                            img_count += 1
                            _render_iiif_batch_progress(
                                done_pages=img_count,
                                total_pages=total_pages,
                                bytes_downloaded=iiif_bytes_downloaded,
                                bytes_total_known=iiif_bytes_total_known,
                                current_page=page_num,
                                start_ts=iiif_start_ts,
                            )
                            elapsed_iiif = max(time.time() - iiif_start_ts, 1e-6)
                            _safe_callback(
                                on_task_update,
                                task_id,
                                {
                                    "status": "正在下载",
                                    "progress": (img_count / total_pages) if total_pages else None,
                                    "speed_text": f"{_format_bytes(iiif_bytes_downloaded / elapsed_iiif)}/s",
                                },
                            )
                        except Exception as e:
                            _emit(
                                logger,
                                f"  -> ⚠️ [打工人] IIIF 第 {page_num} 页下载/转换失败，已跳过。报错: {e}",
                                logging.WARNING,
                            )
                            iiif_err_path = os.path.join(os.path.dirname(save_path), "IIIF_Error_Log.txt")
                            with open(iiif_err_path, "a", encoding="utf-8") as ef:
                                ef.write(
                                    f"{datetime.now().isoformat(timespec='seconds')} | "
                                    f"史料名：{doc_title} | 损坏页码：第 {page_num} 页 | 报错信息：{e}\n"
                                )
                            continue
                    if stop_event.is_set() or aborted_iiif:
                        _mark_task_terminal_state(
                            "aborted",
                            _event_message("A_MANUAL_STOP_IIIF"),
                            doc_status="discovered",
                        )
                        _emit(
                            logger,
                            f"  -> 📌 [打工人] IIIF 已保存断点：{img_count}/{total_pages} 页（复用 {iiif_resume_hits} 页）",
                            logging.INFO,
                        )
                        _safe_callback(
                            on_task_update,
                            task_id,
                            {"status": "已中止", "progress": None, "speed_text": "0.00B/s"},
                        )
                        continue
                    if img_count < total_pages:
                        _mark_task_terminal_state(
                            "failed",
                            _event_message("E_TOYO_IIIF_INCOMPLETE", f"{img_count}/{total_pages}"),
                            doc_status="failed",
                        )
                        _emit(
                            logger,
                            f"  -> ⚠️ [打工人] IIIF 未完成（{img_count}/{total_pages} 页），已保留断点，下次将续传。",
                            logging.WARNING,
                        )
                        continue
                    if img_count == 0:
                        _emit(logger, "  -> ⚠️ [打工人] 未提取到可用图像页，跳过。", logging.WARNING)
                        _mark_task_terminal_state(
                            "failed",
                            _event_message("E_TOYO_IIIF_ZERO_PAGES"),
                            doc_status="failed",
                        )
                        continue
                    pdf_doc.save(save_path)
                finally:
                    pdf_doc.close()
                _write_sidecar_metadata(save_path, doc_metadata, logger)
                db_service.mark_downloaded_with_files(
                    source=source,
                    native_id=native_id,
                    pdf_path=save_path,
                    sidecar_path=os.path.splitext(save_path)[0] + ".json",
                )
                db_service.add_download_event(
                    native_id,
                    "succeeded",
                    run_id=int(run_id) if run_id is not None else None,
                    source=source,
                )
                try:
                    shutil.rmtree(resume_dir, ignore_errors=True)
                except Exception:
                    pass
                _emit(
                    logger,
                    f"  -> ✅ [打工人完工] IIIF 组装成功，共 {img_count} 页（复用断点 {iiif_resume_hits} 页），并已保存同格式 Sidecar 元数据 JSON: {os.path.basename(save_path)}",
                )
                task_success = True
                _safe_callback(
                    on_task_update,
                    task_id,
                    {"status": "已下载", "progress": 1.0, "speed_text": "完成"},
                )
                continue

            # ------------------------------------------
            # 分支 C：胡佛研究所宕机防御战术
            # ------------------------------------------
            elif "hojishinbun.hoover.org" in viewer_url:
                _emit(logger, "  -> ⚠️ 检测到胡佛研究所链接，当前服务器宕机，暂时跳过。", logging.WARNING)
                pending_path = os.path.join(os.path.dirname(save_path), "Hoover_Pending_Tasks.txt")
                sidecar_path = os.path.splitext(save_path)[0] + ".json"
                pending_exists = _hoover_pending_has_url(pending_path, viewer_url)
                sidecar_exists = os.path.exists(sidecar_path)

                if pending_exists and sidecar_exists:
                    db_service.upsert_hoover_pending(
                        native_id=native_id,
                        title=doc_title,
                        viewer_url=viewer_url,
                        metadata=doc_metadata,
                        search_keyword=str(task.get("search_keyword") or ""),
                        sidecar_path=sidecar_path,
                    )
                    db_service.add_download_event(
                        native_id,
                        "succeeded",
                        message="hoover pending already exists",
                        run_id=int(run_id) if run_id is not None else None,
                        source="hoover",
                    )
                    _emit(logger, "  -> ⏭️ 胡佛链接与Sidecar均已存在，跳过重复登记。", logging.INFO)
                    task_success = True
                    _safe_callback(
                        on_task_update,
                        task_id,
                        {"status": "已下载", "progress": 1.0, "speed_text": "N/A"},
                    )
                    continue

                if not pending_exists:
                    with open(pending_path, "a", encoding="utf-8") as pf:
                        pf.write(f"{doc_title} | {viewer_url}\n")
                hoover_metadata = dict(doc_metadata)
                hoover_metadata["Source_URL"] = viewer_url
                hoover_metadata["Download_Status"] = "pending_hoover_unavailable"
                hoover_metadata["Download_Note"] = "Hoover source currently unavailable; download deferred."
                if not sidecar_exists:
                    _write_sidecar_metadata(save_path, hoover_metadata, logger)
                    _emit(
                        logger,
                        f"  -> 🧾 已为胡佛任务写入同格式 Sidecar 元数据 JSON: {os.path.basename(save_path)}",
                    )
                else:
                    _emit(logger, "  -> 🧾 胡佛任务 Sidecar 已存在，跳过重复写入。", logging.INFO)
                db_service.upsert_hoover_pending(
                    native_id=native_id,
                    title=doc_title,
                    viewer_url=viewer_url,
                    metadata=hoover_metadata,
                    search_keyword=str(task.get("search_keyword") or ""),
                    sidecar_path=sidecar_path if os.path.exists(sidecar_path) else None,
                )
                db_service.add_download_event(
                    native_id,
                    "succeeded",
                    message="hoover pending",
                    run_id=int(run_id) if run_id is not None else None,
                    source="hoover",
                )
                task_success = True
                _safe_callback(
                    on_task_update,
                    task_id,
                    {"status": "已下载", "progress": 1.0, "speed_text": "N/A"},
                )
                continue

            # ------------------------------------------
            # 未识别域名
            # ------------------------------------------
            else:
                _emit(logger, f"  -> ⚠️ [打工人] 未支持的域名，跳过: {viewer_url}", logging.WARNING)
                _mark_task_terminal_state(
                    "failed",
                    _event_message("E_UNSUPPORTED_DOMAIN", viewer_url),
                    doc_status="failed",
                )

        except Exception as e:
            _emit(logger, f"  -> ❌ [打工人报错] {doc_title} 处理失败: {e}", logging.ERROR)
            db_service.mark_document_status(source, native_id, "failed")
            db_service.add_download_event(
                native_id,
                "failed",
                message=_event_message("E_TASK_EXCEPTION", str(e)),
                run_id=int(run_id) if run_id is not None else None,
                source=source,
            )
            _safe_callback(
                on_task_update,
                task_id,
                {"status": "失败", "progress": None, "speed_text": "0.00B/s"},
            )
        finally:
            if progress_state is not None:
                lock = progress_state.get("lock")
                if lock is not None:
                    with lock:
                        progress_state["completed"] = int(progress_state.get("completed", 0)) + 1
                        if task_success:
                            progress_state["succeeded"] = int(progress_state.get("succeeded", 0)) + 1
                        else:
                            progress_state["failed"] = int(progress_state.get("failed", 0)) + 1
                        progress_state["bytes_downloaded"] = int(progress_state.get("bytes_downloaded", 0)) + int(task_bytes_downloaded)
                        progress_state["bytes_total_known"] = int(progress_state.get("bytes_total_known", 0)) + int(task_bytes_total_known)
                _render_global_progress(progress_state)
            task_queue.task_done()

# ==========================================
# 👷 包工头：单一 Selenium 控制翻页与发布任务
# ==========================================
def jacar_auto_search(
    target_keyword,
    start_year,
    end_year,
    update_gui_progress,
    finish_scraping,
    stop_event,
    headless=False,
    max_downloads: int | None = None,
    enable_run_log: bool = True,
    strict_row_validation: bool = False,
    on_task_enqueued=None,
    on_task_update=None,
    on_run_started=None,
):
    print("正在初始化网络环境与高并发队列...")
    db_service = DbService()

    if getattr(sys, "frozen", False):
        application_path = os.path.dirname(sys.executable)
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))

    base_download_dir = os.path.join(application_path, "JACAR_Downloads")
    download_dir = os.path.join(base_download_dir, target_keyword)
    os.makedirs(download_dir, exist_ok=True)

    logger: logging.Logger | None = None
    log_path = ""
    failed_rows_path = ""
    if enable_run_log:
        try:
            log_root = os.path.join(application_path, "Scraper_Logs")
            logger, log_path = _build_run_logger(log_root, target_keyword)
            _emit(logger, f"🧾 运行日志已开启：{log_path}")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_kw = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", target_keyword).strip("_") or "kw"
            failed_rows_path = os.path.join(log_root, f"failed_rows_{safe_kw}_{ts}.jsonl")
            _emit(logger, f"🗂️ 失败行证据文件：{failed_rows_path}")
        except Exception as e:
            logger = None
            log_path = ""
            print(f"⚠️ 日志初始化失败，降级为仅终端输出：{e}")

    task_queue = queue.Queue()
    progress_state: dict = {
        "lock": threading.Lock(),
        "start_ts": time.time(),
        "dispatched": 0,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "bytes_downloaded": 0,
        "bytes_total_known": 0,
    }
    workers = []
    for _ in range(3):
        t = threading.Thread(
            target=api_download_worker,
            args=(task_queue, stop_event, logger, progress_state, on_task_update),
            daemon=True,
        )
        t.start()
        workers.append(t)

    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 25)
    if max_downloads is not None:
        max_downloads = max(1, int(max_downloads))

    if max_downloads is None:
        _emit(logger, "🚀 当前为全量下载模式（不设下载上限）。")
    else:
        _emit(logger, f"🧪 当前为测试下载模式，最大下载数：{max_downloads}")
    _emit(logger, f"🔎 严格行校验模式：{'开启' if strict_row_validation else '关闭'}")
    run_id: int | None = None
    try:
        run_id = db_service.begin_download_run(
            keyword=str(target_keyword),
            year_from=str(start_year),
            year_to=str(end_year),
            notes=f"headless={bool(headless)}; strict_row_validation={bool(strict_row_validation)}",
        )
        _safe_callback(on_run_started, run_id)
    except Exception as e:
        _emit(logger, f"⚠️ download_runs 初始化失败（降级继续抓取）: {e}", logging.WARNING)

    try:
        # 1) 直接拼接 URL 访问列表页（不再模拟首页点击）
        base_url = "https://www.jacar.archives.go.jp/aj/search"
        query = urlencode(
            {
                "kl0": "AND",
                "ks0": "kw_all",
                "kw0": target_keyword,
                "date_y_from": start_year,
                "date_y_to": end_year,
                "rows": "200",
                "sf": "seq_a",
            }
        )
        final_url = f"{base_url}?{query}"
        _emit(logger, f"🌐 直接访问检索URL：{final_url}")
        driver.get(final_url)
        _wait_dom_ready(driver, timeout=25)

        # 2) 列表页循环：只处理带“閲覧”按钮的可下载条目
        current_page = 1
        total_tasks_added = 0
        skipped_existing = 0
        skipped_duplicate = 0
        sidecar_only_added = 0
        failed_rows_count = 0
        scheduled_save_paths: set[str] = set()
        scheduled_viewer_urls: set[str] = set()
        while True:
            if stop_event.is_set() or (max_downloads is not None and total_tasks_added >= max_downloads):
                break

            _emit(logger, f"\n========== 🚀 正在解析第 {current_page} 页 ==========")
            no_result_text = "該当する文書が見つかりませんでした"
            try:
                # 等待“有结果行”或“明确无结果提示”任一出现
                wait.until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "li.archive-result-table__body-item")
                    or (no_result_text in (d.page_source or ""))
                )
            except TimeoutException:
                # 若页面结构变化导致无法命中两者，交由外层异常路径处理
                raise

            if no_result_text in (driver.page_source or ""):
                msg = (
                    "⚠️ 未检索到符合条件的史料（該当する文書が見つかりませんでした）。\n"
                    "请修改关键词或扩大年份范围后重试。"
                )
                _emit(logger, msg, logging.WARNING)
                if log_path:
                    msg += f"\n运行日志：{log_path}"
                with _PRINT_LOCK:
                    _clear_status_line_locked()
                finish_scraping(msg)
                return

            rows = driver.find_elements(By.CSS_SELECTOR, "li.archive-result-table__body-item")
            if not rows:
                _emit(logger, "⚠️ 当前页未检测到结果行。", logging.WARNING)
                break

            total_rows = len(rows)
            for idx, row in enumerate(rows, start=1):
                if stop_event.is_set():
                    break
                update_gui_progress(
                    idx,
                    total_rows,
                    f"正在解析第 {current_page} 页 ({idx}/{total_rows}) | 后台积压任务: {task_queue.qsize()} 份",
                )

                try:
                    # 空壳过滤：无“閲覧”按钮即簿冊容器，跳过
                    view_links = row.find_elements(By.CSS_SELECTOR, "a.result-image-link")
                    if not view_links:
                        continue

                    viewer_url = (view_links[0].get_attribute("href") or "").strip()
                    if not viewer_url:
                        continue

                    # 直刮元数据：标题 / Ref / 規模 / 面包屑
                    try:
                        doc_title = row.find_element(By.CSS_SELECTOR, "h3.result-header__title a").text.strip()
                    except Exception:
                        doc_title = "未命名史料"

                    try:
                        ref_code = row.find_element(
                            By.XPATH,
                            ".//dt[contains(normalize-space(.), 'レファレンスコード')]/following-sibling::dd[1]",
                        ).text.strip()
                    except Exception:
                        ref_code = "Unknown_Ref"

                    is_hoover_link = "hojishinbun.hoover.org" in viewer_url
                    source_for_task = "hoover" if is_hoover_link else "jacar"

                    # 极速 DB 过滤：命中已处理状态则直接跳过（不做物理文件 exists 检查）
                    if ref_code and ref_code != "Unknown_Ref":
                        status_in_db = db_service.get_document_status(source_for_task, ref_code)
                        skip_statuses = {"completed", "downloaded"}
                        if source_for_task == "hoover":
                            skip_statuses.add("pending_hoover")
                        if (status_in_db or "").strip().lower() in skip_statuses:
                            task_id = f"{source_for_task}:{ref_code}"
                            _safe_callback(
                                on_task_enqueued,
                                {
                                    "task_id": task_id,
                                    "title": doc_title,
                                    "save_path": "",
                                    "mode": "download_and_sidecar",
                                    "status": "已下载",
                                    "progress": 1.0,
                                    "speed_text": "完成",
                                },
                            )
                            skipped_existing += 1
                            _emit(logger, f"  -> ⏭️ DB命中已完成状态，跳过: {doc_title}", logging.INFO)
                            continue

                    try:
                        scale_raw = row.find_element(
                            By.XPATH,
                            ".//dt[contains(normalize-space(.), '規模')]/following-sibling::dd[1]",
                        ).text.strip()
                    except Exception:
                        scale_raw = "1"
                    scale = _extract_first_digits(scale_raw, default="1")

                    tree_items = row.find_elements(By.CSS_SELECTOR, "ol.result-tree li")
                    tree_texts = [x.text.strip() for x in tree_items if x.text.strip()]
                    repo_name = tree_texts[0] if tree_texts else "未知馆藏"
                    level2_name = tree_texts[1] if len(tree_texts) >= 2 else (tree_texts[0] if tree_texts else "未知层级")
                    parent_name = tree_texts[-1] if tree_texts else "未知分类"
                    if "外務省外交史料館" in repo_name:
                        repo_name = "日本外交史料館"

                    # 全量元数据抽取（列表页 <dl> 结构）
                    doc_metadata: dict[str, str] = {
                        "Title": doc_title,
                        "Ref_Code": ref_code,
                        "Level2_Name": level2_name,
                        "Parent_Name": parent_name,
                        "Repo_Name": repo_name,
                    }
                    dl_nodes = row.find_elements(By.CSS_SELECTOR, "dl")
                    for dl in dl_nodes:
                        # 每个 dl 下通常是若干 div，每个 div 含 dt/dd 键值
                        kv_blocks = dl.find_elements(By.CSS_SELECTOR, "div")
                        for block in kv_blocks:
                            try:
                                dt = block.find_element(By.CSS_SELECTOR, "dt").text.strip()
                                dd = block.find_element(By.CSS_SELECTOR, "dd").text.strip()
                                if not dt or not dd:
                                    continue
                                clean_key = re.sub(r"^\[|\]$", "", dt).strip()
                                if clean_key:
                                    doc_metadata[clean_key] = dd
                            except Exception:
                                # 单个字段缺失不影响整行解析
                                continue

                    if strict_row_validation:
                        missing = []
                        if not viewer_url:
                            missing.append("viewer_url")
                        if doc_title == "未命名史料":
                            missing.append("doc_title")
                        if ref_code == "Unknown_Ref":
                            missing.append("ref_code")
                        if repo_name == "未知馆藏":
                            missing.append("repo_name")
                        if level2_name == "未知层级":
                            missing.append("level2_name")
                        if parent_name == "未知分类":
                            missing.append("parent_name")
                        if missing:
                            failed_rows_count += 1
                            _emit(
                                logger,
                                f"  -> ⚠️ 严格校验未通过，跳过该行（缺失字段: {', '.join(missing)}）",
                                logging.WARNING,
                            )
                            if failed_rows_path:
                                _append_jsonl(
                                    failed_rows_path,
                                    {
                                        "ts": datetime.now().isoformat(timespec="seconds"),
                                        "reason": "strict_validation_failed",
                                        "missing_fields": missing,
                                        "page_index": current_page,
                                        "row_index": idx,
                                        "doc_title": doc_title,
                                        "viewer_url": viewer_url,
                                        "ref_code": ref_code,
                                        "repo_name": repo_name,
                                        "level2_name": level2_name,
                                        "parent_name": parent_name,
                                        "scale": scale,
                                    },
                                )
                            db_service.add_failed_row(
                                run_id=run_id,
                                reason="strict_validation_failed",
                                page_index=current_page,
                                row_index=idx,
                                payload={
                                    "doc_title": doc_title,
                                    "viewer_url": viewer_url,
                                    "ref_code": ref_code,
                                    "repo_name": repo_name,
                                    "level2_name": level2_name,
                                    "parent_name": parent_name,
                                    "scale": scale,
                                    "missing_fields": missing,
                                },
                            )
                            continue

                    raw_target_name = (
                        f"{level2_name}：「{doc_title}」、JACAR Ref. {ref_code}"
                        f"（第1—{scale}画像目）、『{parent_name}』（{repo_name}）"
                    )
                    safe_target_name = _sanitize_filename(raw_target_name)
                    final_save_path = os.path.join(download_dir, safe_target_name + ".pdf")
                    task_id = f"{source_for_task}:{ref_code}" if ref_code and ref_code != "Unknown_Ref" else final_save_path

                    # 监控列表：在遍历到结果行时就建立条目，不依赖是否入队下载
                    _safe_callback(
                        on_task_enqueued,
                        {
                            "task_id": task_id,
                            "title": doc_title,
                            "save_path": final_save_path,
                            "mode": "download_and_sidecar",
                            "status": "待下载",
                            "progress": 0.0,
                            "speed_text": "0.00B/s",
                        },
                    )

                    # 去重机制 2：同一轮解析中避免重复入队（按 save_path + viewer_url 双重判重）。
                    if final_save_path in scheduled_save_paths or viewer_url in scheduled_viewer_urls:
                        skipped_duplicate += 1
                        _emit(
                            logger,
                            f"  -> ⏭️ 本轮已入队重复项，跳过: {doc_title}",
                            logging.INFO,
                        )
                        continue

                    # 状态入库（不存在/待处理/失败时 upsert 到 discovered，再交给打工人）
                    db_service.upsert_document(
                        source=source_for_task,
                        native_id=ref_code,
                        title=doc_title,
                        repo_name=repo_name,
                        level2_name=level2_name,
                        parent_name=parent_name,
                        scale=scale,
                        viewer_url=viewer_url,
                        search_keyword=target_keyword,
                        metadata=doc_metadata,
                        status="discovered",
                    )

                    task_queue.put(
                        {
                            "url": viewer_url,
                            "save_path": final_save_path,
                            "title": doc_title,
                            "metadata": doc_metadata,
                            "mode": "download_and_sidecar",
                            "task_id": task_id,
                            "source": source_for_task,
                            "native_id": ref_code,
                            "search_keyword": target_keyword,
                            "run_id": run_id,
                        }
                    )
                    db_service.add_download_event(
                        ref_code,
                        "queued",
                        run_id=run_id,
                        source=source_for_task,
                    )
                    scheduled_save_paths.add(final_save_path)
                    scheduled_viewer_urls.add(viewer_url)
                    total_tasks_added += 1
                    with progress_state["lock"]:
                        progress_state["dispatched"] = int(progress_state.get("dispatched", 0)) + 1
                    _render_global_progress(progress_state)
                    _emit(
                        logger,
                        f"  -> 📦 下载+Sidecar 任务已分发: {doc_title} | 当前积压: {task_queue.qsize()}",
                    )
                    if max_downloads is not None and total_tasks_added >= max_downloads:
                        _emit(logger, f"\n🎯 已达到测试上限 {max_downloads} 份，停止继续分发。")
                        break
                except Exception as row_e:
                    _emit(logger, f"  -> ⚠️ 行解析失败，已跳过。简略报错: {str(row_e).splitlines()[0]}", logging.WARNING)
                    failed_rows_count += 1
                    row_text = ""
                    if failed_rows_path:
                        try:
                            row_text = row.text.strip()[:400]
                        except Exception:
                            row_text = ""
                        _append_jsonl(
                            failed_rows_path,
                            {
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "reason": "row_parse_exception",
                                "page_index": current_page,
                                "row_index": idx,
                                "error": str(row_e),
                                "row_text_preview": row_text,
                            },
                        )
                    db_service.add_failed_row(
                        run_id=run_id,
                        reason="row_parse_exception",
                        page_index=current_page,
                        row_index=idx,
                        payload={
                            "error": str(row_e),
                            "row_text_preview": row_text,
                        },
                    )
                    continue

            # 3) 翻页：存在下一页按钮则继续，否则结束
            try:
                if max_downloads is not None and total_tasks_added >= max_downloads:
                    break
                next_candidates = driver.find_elements(
                    By.XPATH,
                    (
                        "//a[contains(@class,'pagination-next') and not(contains(@class,'disabled'))] | "
                        "//a[@rel='next'] | "
                        "//a[contains(., '次') and not(contains(@class,'disabled'))]"
                    ),
                )
                next_btn = next_candidates[0] if next_candidates else None
                if not next_btn:
                    _emit(logger, "\n🛑 未找到可点击的下一页按钮，列表遍历结束。")
                    break
                first_row = rows[0]
                driver.execute_script("arguments[0].click();", next_btn)
                wait.until(EC.staleness_of(first_row))
                _wait_dom_ready(driver, timeout=25)
                current_page += 1
            except Exception:
                _emit(logger, "\n🛑 下一页翻页失败，列表遍历结束。", logging.WARNING)
                break

        # 4) 等待后台任务清空
        if not stop_event.is_set():
            update_gui_progress(0, 0, "⏳ 列表已解析完成，正在等待后台线程下载剩余史料...")
            _emit(logger, "\n⏳ 正在等待后台打工人清理积压任务...")
            while not task_queue.empty() or task_queue.unfinished_tasks > 0:
                if stop_event.is_set():
                    break
                time.sleep(1)

        if stop_event.is_set():
            msg = (
                "🛑 任务已被手动终止。部分文件可能未下载。"
                f"\n统计：分发={total_tasks_added}（其中仅补写Sidecar={sidecar_only_added}），本地完整存在跳过={skipped_existing}，本轮重复跳过={skipped_duplicate}，失败行={failed_rows_count}"
            )
            msg += (
                f"\n下载统计：总下载量={_format_bytes(progress_state.get('bytes_downloaded', 0))}，"
                f"已知总量={_format_bytes(progress_state.get('bytes_total_known', 0)) if int(progress_state.get('bytes_total_known', 0)) > 0 else '未知'}，"
                f"完成={progress_state.get('completed', 0)}/{progress_state.get('dispatched', 0)}"
            )
            if log_path:
                msg += f"\n运行日志：{log_path}"
            if failed_rows_path and failed_rows_count > 0:
                msg += f"\n失败行证据：{failed_rows_path}"
            with _PRINT_LOCK:
                _clear_status_line_locked()
            if run_id is not None:
                db_service.finish_download_run(
                    run_id,
                    dispatched=total_tasks_added,
                    completed=int(progress_state.get("completed", 0)),
                    succeeded=int(progress_state.get("succeeded", 0)),
                    failed=int(progress_state.get("failed", 0)),
                    sidecar_only=sidecar_only_added,
                    notes="stopped",
                )
            finish_scraping(msg)
        else:
            msg = (
                f"🎉 抓取任务完成！共分发任务 {total_tasks_added} 份。"
                f"\n统计：其中仅补写Sidecar={sidecar_only_added}，本地完整存在跳过={skipped_existing}，本轮重复跳过={skipped_duplicate}，失败行={failed_rows_count}"
            )
            msg += (
                f"\n下载统计：总下载量={_format_bytes(progress_state.get('bytes_downloaded', 0))}，"
                f"已知总量={_format_bytes(progress_state.get('bytes_total_known', 0)) if int(progress_state.get('bytes_total_known', 0)) > 0 else '未知'}，"
                f"完成={progress_state.get('completed', 0)}/{progress_state.get('dispatched', 0)}"
            )
            if log_path:
                msg += f"\n运行日志：{log_path}"
            if failed_rows_path and failed_rows_count > 0:
                msg += f"\n失败行证据：{failed_rows_path}"
            with _PRINT_LOCK:
                _clear_status_line_locked()
            if run_id is not None:
                db_service.finish_download_run(
                    run_id,
                    dispatched=total_tasks_added,
                    completed=int(progress_state.get("completed", 0)),
                    succeeded=int(progress_state.get("succeeded", 0)),
                    failed=int(progress_state.get("failed", 0)),
                    sidecar_only=sidecar_only_added,
                    notes="completed",
                )
            finish_scraping(msg)

    except Exception:
        _emit(logger, "====== 🚨 发生致命错误 ======", logging.ERROR)
        traceback.print_exc()
        msg = "❌ 发生致命错误，请查看终端日志。"
        if log_path:
            msg += f"\n运行日志：{log_path}"
        with _PRINT_LOCK:
            _clear_status_line_locked()
        if run_id is not None:
            sidecar_only_value = int(locals().get("sidecar_only_added", 0))
            db_service.finish_download_run(
                run_id,
                dispatched=0,
                completed=int(progress_state.get("completed", 0)),
                succeeded=int(progress_state.get("succeeded", 0)),
                failed=int(progress_state.get("failed", 0)),
                sidecar_only=sidecar_only_value,
                notes="fatal_error",
            )
        finish_scraping(msg)
    finally:
        for _ in workers:
            task_queue.put(None)
        for t in workers:
            try:
                t.join(timeout=3)
            except Exception:
                pass
        time.sleep(1)
        try:
            driver.quit()
        except Exception:
            pass
        if logger is not None:
            for h in list(logger.handlers):
                try:
                    h.close()
                except Exception:
                    pass
                logger.removeHandler(h)