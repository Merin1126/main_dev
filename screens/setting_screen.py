import customtkinter as ctk
import os
import threading
from datetime import datetime, timezone
from tkinter import messagebox

from components.ui.button import Button
from config.settings import Color
from screens.document_catalog_window import DocumentCatalogWindow
from screens.report_summary_window import ReportSummaryWindow
from services import LlmService
from services.db_disk_sync_service import DbDiskSyncService
from services.sidecar_filename_sync_service import SidecarFilenameSyncService
from utils.trace_report import convert_current_trace_to_md, delete_current_converted_trace_md
from config.api_key_store import (
    load_google_api_key as load_gemini_api_key,
    save_google_api_key as save_gemini_api_key,
    clear_google_api_key as clear_gemini_api_key,
    load_ocr_preprocess_config,
    save_ocr_preprocess_config,
    load_trace_config,
    save_trace_config,
    mask_api_key,
)
from config.settings import OCR_PREPROCESS_ENABLED, OCR_PREPROCESS_MODE


class SettingScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT, **kwargs)
        self.master = master
        self.project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.report_window = None
        self.catalog_window = None
        self._setup_ui()
        self._load_config()

    def _setup_ui(self):
        container = ctk.CTkScrollableFrame(self, corner_radius=10)
        container.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            container,
            text="\U0000E690 系统与环境设置",
            font=("Symbols Nerd Font", 22, "bold")
        ).pack(anchor="w", padx=16, pady=(16, 8))

        ctk.CTkLabel(
            container,
            text="Google Gemini API Key（本地保存，不写入代码）",
            font=("Arial", 14)
        ).pack(anchor="w", padx=16, pady=(8, 6))

        self.api_entry = ctk.CTkEntry(
            container,
            width=520,
            height=40,
            show="*",
            placeholder_text="请输入 Google Gemini API Key"
        )
        self.api_entry.pack(anchor="w", padx=16, pady=(0, 8))

        self.api_hint_label = ctk.CTkLabel(
            container,
            text="当前状态：未配置",
            font=("Arial", 12)
        )
        self.api_hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        Button(
            container,
            text="保存 API Key",
            width=180,
            height=40,
            command=self.save_api_key
        ).pack(anchor="w", padx=16, pady=(0, 10))

        Button(
            container,
            text="清空 API Key",
            width=180,
            height=40,
            fg_color=Color.BTN_WARNING,
            hover_color=Color.BTN_WARNING_HOVER,
            command=self.clear_api_key
        ).pack(anchor="w", padx=16, pady=(0, 10))

        ctk.CTkLabel(
            container,
            text="建议：优先使用环境变量；若必须本地保存，请不要共享项目目录与 .secrets 文件。",
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE
        ).pack(anchor="w", padx=16, pady=(2, 16))

        ctk.CTkLabel(
            container,
            text="Analysis 云端 Context Cache（explicit cache）",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(8, 6))

        ctk.CTkLabel(
            container,
            text="查询当前 Google 账号下仍生效的上下文缓存。未删除的 cache 可能继续产生存储费用。",
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="检测活动中的云端 Cache",
            width=240,
            height=40,
            command=self.check_active_context_caches,
        ).pack(anchor="w", padx=16, pady=(0, 8))

        self.cache_check_hint_label = ctk.CTkLabel(
            container,
            text="尚未检测。",
            font=("Arial", 12),
            wraplength=560,
            justify="left",
        )
        self.cache_check_hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        ctk.CTkLabel(
            container,
            text="Gemini 调用追踪（本地 JSONL 日志）",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(8, 6))

        self.trace_enabled_var = ctk.BooleanVar(value=False)
        self.trace_full_text_var = ctk.BooleanVar(value=True)
        self.ocr_preprocess_enabled_var = ctk.BooleanVar(value=OCR_PREPROCESS_ENABLED)
        self.ocr_preprocess_mode_var = ctk.StringVar(value=OCR_PREPROCESS_MODE)

        ctk.CTkCheckBox(
            container,
            text="启用追踪日志（记录请求 Prompt、模型响应、缓存落盘）",
            variable=self.trace_enabled_var,
        ).pack(anchor="w", padx=16, pady=(0, 6))

        ctk.CTkCheckBox(
            container,
            text="记录完整文本（关闭则仅保留预览片段）",
            variable=self.trace_full_text_var,
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="保存追踪设置",
            width=180,
            height=40,
            command=self.save_trace_settings,
        ).pack(anchor="w", padx=16, pady=(0, 10))

        self.trace_hint_label = ctk.CTkLabel(
            container,
            text="追踪状态：未启用",
            font=("Arial", 12),
        )
        self.trace_hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        ctk.CTkLabel(
            container,
            text="OCR 图像预处理（识别前增强）",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(8, 6))

        ctk.CTkCheckBox(
            container,
            text="启用 OCR 图像预处理",
            variable=self.ocr_preprocess_enabled_var,
        ).pack(anchor="w", padx=16, pady=(0, 6))

        ctk.CTkLabel(
            container,
            text="增强档位",
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE,
        ).pack(anchor="w", padx=16, pady=(0, 4))

        self.ocr_preprocess_mode_menu = ctk.CTkOptionMenu(
            container,
            width=180,
            values=["off", "mild", "strong"],
            variable=self.ocr_preprocess_mode_var,
        )
        self.ocr_preprocess_mode_menu.pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="保存 OCR 预处理设置",
            width=220,
            height=40,
            command=self.save_ocr_preprocess_settings,
        ).pack(anchor="w", padx=16, pady=(0, 10))

        self.ocr_preprocess_hint_label = ctk.CTkLabel(
            container,
            text="OCR 预处理：未配置",
            font=("Arial", 12),
        )
        self.ocr_preprocess_hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        Button(
            container,
            text="转化当前的 Trace日志",
            width=220,
            height=40,
            command=self.convert_current_trace_log,
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="删除当前已转化的 Trace 日志",
            width=260,
            height=40,
            fg_color=Color.BTN_WARNING,
            hover_color=Color.BTN_WARNING_HOVER,
            command=self.delete_current_trace_report,
        ).pack(anchor="w", padx=16, pady=(0, 12))

        ctk.CTkLabel(
            container,
            text="SQLite 数据库",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(10, 6))

        ctk.CTkLabel(
            container,
            text=(
                "对照 JACAR_Downloads 目录下的 PDF 重新同步 documents / files 表。"
                "适用于重命名史料后手动刷新标题、路径与索引（不会删除既有 sidecar 记录）。"
            ),
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="刷新 SQL 数据库（对照磁盘）",
            width=280,
            height=40,
            command=self.refresh_sql_database_from_disk,
        ).pack(anchor="w", padx=16, pady=(0, 8))

        self.db_sync_hint_label = ctk.CTkLabel(
            container,
            text="尚未执行同步。",
            font=("Arial", 12),
            wraplength=560,
            justify="left",
        )
        self.db_sync_hint_label.pack(anchor="w", padx=16, pady=(0, 8))

        ctk.CTkLabel(
            container,
            text=(
                "若已在 Finder 中改过 PDF 文件名，可先将 JACAR_Downloads 下旧的 .json sidecar "
                "重命名为与 PDF 同名（按 JACAR Ref 配对），再执行上方的 SQL 刷新。"
            ),
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="同步 JSON 文件名（全盘）",
            width=280,
            height=40,
            command=self.sync_sidecar_json_names,
        ).pack(anchor="w", padx=16, pady=(0, 8))

        self.sidecar_sync_hint_label = ctk.CTkLabel(
            container,
            text="尚未执行 JSON 同步。",
            font=("Arial", 12),
            wraplength=560,
            justify="left",
        )
        self.sidecar_sync_hint_label.pack(anchor="w", padx=16, pady=(0, 8))

        ctk.CTkLabel(
            container,
            text="浏览、筛选与重命名 SQLite 中的 JACAR 条目，并导出 DOCX/PDF 史料目录。",
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="打开史料目录管理",
            width=280,
            height=40,
            command=self.open_document_catalog_window,
        ).pack(anchor="w", padx=16, pady=(0, 14))

        ctk.CTkLabel(
            container,
            text="进度汇报与总结",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(10, 6))

        Button(
            container,
            text="打开总结页面",
            width=220,
            height=40,
            command=self.open_report_window,
        ).pack(anchor="w", padx=16, pady=(0, 12))

    def _load_config(self):
        key = load_gemini_api_key()
        if key:
            self.api_hint_label.configure(text=f"当前状态：已配置（{mask_api_key(key)}）")
        else:
            self.api_hint_label.configure(text="当前状态：未配置")
        trace_cfg = load_trace_config()
        self.trace_enabled_var.set(bool(trace_cfg.get("enabled", False)))
        self.trace_full_text_var.set(bool(trace_cfg.get("include_full_text", True)))
        preprocess_cfg = load_ocr_preprocess_config()
        self.ocr_preprocess_enabled_var.set(bool(preprocess_cfg.get("enabled", OCR_PREPROCESS_ENABLED)))
        self.ocr_preprocess_mode_var.set(str(preprocess_cfg.get("mode", OCR_PREPROCESS_MODE)))
        self._refresh_trace_hint()
        self._refresh_ocr_preprocess_hint()

    def _refresh_trace_hint(self):
        enabled = bool(self.trace_enabled_var.get())
        full_text = bool(self.trace_full_text_var.get())
        if enabled:
            detail = "完整文本" if full_text else "预览片段"
            self.trace_hint_label.configure(
                text=f"追踪状态：已启用（{detail}） | 输出目录：项目根目录/Gemini_Trace"
            )
        else:
            self.trace_hint_label.configure(text="追踪状态：未启用")

    def _refresh_ocr_preprocess_hint(self):
        enabled = bool(self.ocr_preprocess_enabled_var.get())
        mode = str(self.ocr_preprocess_mode_var.get()).strip().lower()
        if mode not in {"off", "mild", "strong"}:
            mode = OCR_PREPROCESS_MODE
            self.ocr_preprocess_mode_var.set(mode)
        if not enabled:
            self.ocr_preprocess_hint_label.configure(text="OCR 预处理：已关闭（等同 off）")
            return
        if mode == "off":
            mode_desc = "off（不增强）"
        elif mode == "strong":
            mode_desc = "strong（对比 1.5 / 锐度 1.2）"
        else:
            mode_desc = "mild（推荐；对比 1.3 / 锐度 1.1）"
        self.ocr_preprocess_hint_label.configure(text=f"OCR 预处理：已启用，当前 {mode_desc}")

    def save_api_key(self):
        raw_key = self.api_entry.get().strip()
        if not raw_key:
            messagebox.showwarning("提示", "请输入 API Key 后再保存。")
            return
        save_gemini_api_key(raw_key)
        self.api_entry.delete(0, "end")
        self.api_hint_label.configure(text=f"当前状态：已配置（{mask_api_key(raw_key)}）")
        messagebox.showinfo("成功", "Gemini API Key 已保存到本地安全配置。")

    @staticmethod
    def _format_cache_expire_time(expire_time) -> str:
        if expire_time is None:
            return "未知"
        if isinstance(expire_time, str):
            text = expire_time.strip()
            return text if text else "未知"
        iso = getattr(expire_time, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                return str(expire_time)
        return str(expire_time)

    @staticmethod
    def _cache_remaining_seconds(expire_time) -> int | None:
        text = SettingScreen._format_cache_expire_time(expire_time)
        if not text or text == "未知":
            return None
        try:
            normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
            expire_dt = datetime.fromisoformat(normalized)
        except Exception:
            return None
        if expire_dt.tzinfo is None:
            expire_dt = expire_dt.replace(tzinfo=timezone.utc)
        return int((expire_dt - datetime.now(timezone.utc)).total_seconds())

    def check_active_context_caches(self):
        """后台拉取 `client.caches.list()`，汇总仍生效的 explicit context cache。"""
        api_key = (
            os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
            or load_gemini_api_key()
        )
        if not api_key:
            messagebox.showwarning("提示", "请先配置 Gemini API Key 后再检测云端 Cache。")
            return
        self.cache_check_hint_label.configure(text="正在检测云端 Cache，请稍候…")
        threading.Thread(
            target=self._run_active_context_cache_check,
            args=(api_key,),
            daemon=True,
        ).start()

    def _run_active_context_cache_check(self, api_key: str) -> None:
        try:
            llm = LlmService(api_key=api_key, project_root=self.project_root, timeout_sec=30)
            all_caches = llm.list_context_caches()
            active_rows: list[dict[str, str]] = []
            for cache in all_caches or []:
                remaining = self._cache_remaining_seconds(getattr(cache, "expire_time", None))
                if remaining is not None and remaining <= 0:
                    continue
                display = str(getattr(cache, "display_name", "") or "").strip()
                active_rows.append(
                    {
                        "name": str(getattr(cache, "name", "") or "").strip() or "（无 ID）",
                        "model": str(getattr(cache, "model", "") or "").strip() or "未知",
                        "display_name": display or "（未设置）",
                        "expire_time": self._format_cache_expire_time(getattr(cache, "expire_time", None)),
                        "remaining": (
                            f"{remaining // 60} 分钟" if remaining is not None and remaining >= 0 else "未知"
                        ),
                        "is_analysis": "是" if display.startswith("analysis:") else "否",
                    }
                )

            lines: list[str] = []
            lines.append(f"检测时间（UTC）：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"活动中的 Cache 数量：{len(active_rows)}")
            analysis_count = sum(1 for row in active_rows if row["is_analysis"] == "是")
            lines.append(f"其中本程序 Analysis 创建（display_name 以 analysis: 开头）：{analysis_count}")
            lines.append("")
            if not active_rows:
                lines.append("当前账号下没有检测到仍生效的 explicit context cache。")
                lines.append("（已过期项不会列入；若刚删除，列表可能稍后才会更新。）")
            else:
                for idx, row in enumerate(active_rows, start=1):
                    lines.append(f"【{idx}】")
                    lines.append(f"  缓存名称 (ID)：{row['name']}")
                    lines.append(f"  绑定的模型：{row['model']}")
                    lines.append(f"  display_name：{row['display_name']}")
                    lines.append(f"  到期时间：{row['expire_time']}")
                    lines.append(f"  剩余约：{row['remaining']}")
                    lines.append(f"  是否 Analysis 任务：{row['is_analysis']}")
                    lines.append("-" * 40)

            report = "\n".join(lines)
            summary = (
                f"上次检测：{len(active_rows)} 个活动 cache"
                f"（Analysis：{analysis_count}）"
            )
            self.after(0, lambda: self._apply_cache_check_result(summary, report))
        except Exception as e:
            err = str(e)
            self.after(
                0,
                lambda: self._apply_cache_check_error(err),
            )

    def _apply_cache_check_result(self, summary: str, report: str) -> None:
        self.cache_check_hint_label.configure(text=summary)
        self._show_cache_check_dialog("云端 Context Cache 检测结果", report)

    def _apply_cache_check_error(self, err: str) -> None:
        self.cache_check_hint_label.configure(text="检测失败，请查看错误详情。")
        messagebox.showerror("检测失败", f"无法列出云端 Context Cache：\n{err}")

    def _show_cache_check_dialog(self, title: str, body: str) -> None:
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("760x520")
        win.transient(self.winfo_toplevel())
        ctk.CTkLabel(
            win,
            text=title,
            font=("Arial", 15, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 6))
        textbox = ctk.CTkTextbox(win, wrap="word", font=("Menlo", 12))
        textbox.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        textbox.insert("0.0", body)
        textbox.configure(state="disabled")
        Button(
            win,
            text="关闭",
            width=120,
            height=36,
            command=win.destroy,
        ).pack(pady=(0, 12))

    def clear_api_key(self):
        removed = clear_gemini_api_key()
        os.environ.pop("GOOGLE_GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_VISION_API_KEY", None)  # 兼容清理旧变量
        self.api_entry.delete(0, "end")
        self.api_hint_label.configure(text="当前状态：未配置")
        if removed:
            messagebox.showinfo("成功", "已清空本地 Gemini API Key 配置。")
        else:
            messagebox.showinfo("提示", "未检测到本地 Gemini API Key 文件，当前已是未配置状态。")

    def save_trace_settings(self):
        enabled = bool(self.trace_enabled_var.get())
        include_full_text = bool(self.trace_full_text_var.get())
        save_trace_config(enabled=enabled, include_full_text=include_full_text)
        self._refresh_trace_hint()
        messagebox.showinfo("成功", "Gemini 追踪设置已保存。")

    def save_ocr_preprocess_settings(self):
        enabled = bool(self.ocr_preprocess_enabled_var.get())
        mode = str(self.ocr_preprocess_mode_var.get()).strip().lower()
        if mode not in {"off", "mild", "strong"}:
            messagebox.showwarning("提示", "OCR 预处理模式仅支持 off / mild / strong。")
            return
        try:
            save_ocr_preprocess_config(enabled=enabled, mode=mode)
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self._refresh_ocr_preprocess_hint()
        messagebox.showinfo("成功", "OCR 预处理设置已保存。")

    def convert_current_trace_log(self):
        try:
            out = convert_current_trace_to_md(self.project_root)
        except FileNotFoundError as e:
            messagebox.showwarning("提示", str(e))
            return
        except Exception as e:
            messagebox.showerror("错误", f"转换 Trace 失败：{e}")
            return
        messagebox.showinfo("成功", f"Trace 报告已生成：\n{out}")

    def delete_current_trace_report(self):
        try:
            removed = delete_current_converted_trace_md(self.project_root)
        except Exception as e:
            messagebox.showerror("错误", f"删除 Trace 报告失败：{e}")
            return
        if removed is None:
            messagebox.showinfo("提示", "未找到可删除的已转化 Trace 报告（.md）。")
            return
        messagebox.showinfo("成功", f"已删除：\n{removed}")

    def open_report_window(self):
        if self.report_window is not None and self.report_window.winfo_exists():
            self.report_window.lift()
            self.report_window.focus_set()
            return
        self.report_window = ReportSummaryWindow(self, project_root=self.project_root)
        self.report_window.transient(self.winfo_toplevel())
        self.report_window.focus_set()

    def open_document_catalog_window(self) -> None:
        if self.catalog_window is not None and self.catalog_window.winfo_exists():
            self.catalog_window.lift()
            self.catalog_window.focus_set()
            return
        self.catalog_window = DocumentCatalogWindow(self, project_root=self.project_root)
        self.catalog_window.transient(self.winfo_toplevel())
        self.catalog_window.focus_set()

    def refresh_sql_database_from_disk(self) -> None:
        if not messagebox.askyesno(
            "刷新 SQL 数据库",
            "将扫描 JACAR_Downloads 下全部 PDF，并更新 SQLite 中的\n"
            "标题、出处字段与 PDF 路径；对库中标记已下载但磁盘缺失的记录会重置为 discovered。\n\n"
            "是否继续？",
            parent=self.winfo_toplevel(),
        ):
            return
        self.db_sync_hint_label.configure(text="正在对照磁盘刷新数据库，请稍候…")
        threading.Thread(target=self._run_db_disk_sync, daemon=True).start()

    def _run_db_disk_sync(self) -> None:
        try:
            stats = DbDiskSyncService(project_root=self.project_root).sync_from_disk()
            report = "\n".join(stats.summary_lines())
            summary = (
                f"上次同步：扫描 {stats.pdfs_scanned} 个 PDF；"
                f"更新 {stats.documents_updated} / 新建 {stats.documents_created} 条；"
                f"路径 {stats.pdf_paths_updated} 条"
            )
            self.after(0, lambda: self._apply_db_sync_result(summary, report))
        except Exception as exc:
            err = str(exc)
            self.after(0, lambda: self._apply_db_sync_error(err))

    def _apply_db_sync_result(self, summary: str, report: str) -> None:
        self.db_sync_hint_label.configure(text=summary)
        self._show_cache_check_dialog("SQL 数据库同步结果", report)

    def _apply_db_sync_error(self, err: str) -> None:
        self.db_sync_hint_label.configure(text="数据库同步失败，请查看错误详情。")
        messagebox.showerror("同步失败", f"无法刷新 SQL 数据库：\n{err}")

    def sync_sidecar_json_names(self) -> None:
        target = os.path.join(self.project_root, "JACAR_Downloads")
        if not messagebox.askyesno(
            "同步 JSON 文件名",
            f"将递归扫描整个史料库：\n{target}\n\n"
            "按 JACAR Ref 把 sidecar .json 重命名为与同条 PDF 相同的主文件名，"
            "并更新 JSON 内的 Title 等字段。\n\n是否继续？",
            parent=self.winfo_toplevel(),
        ):
            return
        self.sidecar_sync_hint_label.configure(text="正在全盘同步 JSON 文件名，请稍候…")
        threading.Thread(
            target=self._run_sidecar_filename_sync,
            args=(target,),
            daemon=True,
        ).start()

    def _run_sidecar_filename_sync(self, target_dir: str) -> None:
        try:
            stats = SidecarFilenameSyncService(project_root=self.project_root).sync_directory(
                target_dir
            )
            report = "\n".join(stats.summary_lines())
            summary = (
                f"JSON 同步：重命名 {stats.json_renamed} 个，"
                f"已对齐 {stats.already_matched} 个，"
                f"无 JSON 的 PDF {stats.pdfs_without_json} 个"
            )
            self.after(0, lambda: self._apply_sidecar_sync_result(summary, report))
        except Exception as exc:
            err = str(exc)
            self.after(0, lambda: self._apply_sidecar_sync_error(err))

    def _apply_sidecar_sync_result(self, summary: str, report: str) -> None:
        self.sidecar_sync_hint_label.configure(text=summary)
        self._show_cache_check_dialog("Sidecar JSON 文件名同步结果", report)

    def _apply_sidecar_sync_error(self, err: str) -> None:
        self.sidecar_sync_hint_label.configure(text="JSON 同步失败。")
        messagebox.showerror("同步失败", f"无法同步 JSON 文件名：\n{err}")

