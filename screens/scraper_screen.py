import threading
import customtkinter as ctk
from tkinter import messagebox
from config.settings import Color
from services import DbService
from services.mofa_workflow_service import (
    MOFA_MODE_ALL_CONTENT,
    MOFA_MODE_MATCHED,
    MOFA_MODE_SCAN,
    MofaWorkflowResult,
    MofaWorkflowService,
)

# 导入咱们刚移植过来的高级 UI 组件
from components.ui.button import Button
from components.ui.input import Input

# 导入核心爬虫打工人
import core_scraper


SOURCE_JACAR = "JACAR 原始档案"
SOURCE_MOFA = "MOFA 日本外交文書"
MOFA_MODE_LABELS = {
    "仅扫描目录（推荐）": MOFA_MODE_SCAN,
    "下载目录标题命中项": MOFA_MODE_MATCHED,
    "下载范围内全部正文PDF": MOFA_MODE_ALL_CONTENT,
}

class ScraperScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        # 继承大管家的透明背景
        super().__init__(master, fg_color=("#f5f6f8", "#1f1f23"), **kwargs)
        self.master = master
        self.stop_event = threading.Event()
        self.db_service = DbService()
        self.current_run_id: int | None = None
        self.awaiting_run_id = False
        self.monitor_window = None
        self.monitor_rows: dict[str, dict] = {}
        self.monitor_list_frame = None
        self.monitor_title = None
        self.monitor_poll_job = None
        self.mofa_result_window = None

        self._setup_ui()

    def _setup_ui(self):
        # 页面大标题
        title_lbl = ctk.CTkLabel(
            self,
            text="史料下载控制台",
            font=("Arial", 28, "bold"),
            text_color=Color.TEXT,
        )
        title_lbl.pack(pady=(30, 10))

        # 中间的圆角主容器 (Main Card Container)
        container = ctk.CTkScrollableFrame(
            self,
            corner_radius=15,
            fg_color=("#eceff4", "#2b2b31"),
            border_width=1,
            border_color=("#d7dde7", "#3c4452"),
        )
        container.pack(pady=20, padx=40, fill="both", expand=True)

        # ================= 表单输入区 =================
        ctk.CTkLabel(
            container,
            text=" 史料来源:",
            font=("Symbols Nerd Font", 15, "bold"),
        ).pack(pady=(22, 5))
        self.source_var = ctk.StringVar(value=SOURCE_JACAR)
        self.source_menu = ctk.CTkOptionMenu(
            container,
            width=400,
            values=[SOURCE_JACAR, SOURCE_MOFA],
            variable=self.source_var,
            command=self._on_source_changed,
        )
        self.source_menu.pack()

        ctk.CTkLabel(
            container,
            text="\ue68f 检索关键词:",
            font=("Symbols Nerd Font", 15, "bold")
        ).pack(pady=(30, 5))
        # 使用你刚移植的高级 Input 组件
        self.entry_keyword = Input(container, width=400, defaultValue="反帝國主義")
        self.entry_keyword.pack()

        ctk.CTkLabel(
            container,
            text="\U000f0e17 起始年份 (如 1921):",
            font=("Symbols Nerd Font", 15, "bold")
        ).pack(pady=(20, 5))
        self.entry_start_year = Input(container, width=400, defaultValue="1921")
        self.entry_start_year.pack()

        ctk.CTkLabel(
            container,
            text="\U000f0e17 结束年份 (如 1927):",
            font=("Symbols Nerd Font", 15, "bold")
        ).pack(pady=(20, 5))
        self.entry_end_year = Input(container, width=400, defaultValue="1927")
        self.entry_end_year.pack()

        self.mofa_mode_frame = ctk.CTkFrame(container, fg_color=Color.TRANSPARENT)
        ctk.CTkLabel(
            self.mofa_mode_frame,
            text="MOFA 执行方式:",
            font=("Arial", 14, "bold"),
        ).pack(pady=(14, 5))
        self.mofa_mode_var = ctk.StringVar(value=next(iter(MOFA_MODE_LABELS)))
        self.mofa_mode_menu = ctk.CTkOptionMenu(
            self.mofa_mode_frame,
            width=400,
            values=list(MOFA_MODE_LABELS.keys()),
            variable=self.mofa_mode_var,
        )
        self.mofa_mode_menu.pack()
        self.mofa_hint_label = ctk.CTkLabel(
            self.mofa_mode_frame,
            text="默认只建立目录索引，不下载 PDF。全文检索将在 Phase 5 接入。",
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_SOFT,
        )
        self.mofa_hint_label.pack(pady=(6, 0))

        self.headless_var = ctk.BooleanVar(value=False)
        self.headless_checkbox = ctk.CTkCheckBox(
            container,
            text="无头模式（不打开浏览器窗口）",
            variable=self.headless_var,
            font=("Arial", 13),
        )
        self.headless_checkbox.pack(pady=(14, 0))

        # ================= 操作按钮区 =================
        self.btn_frame = ctk.CTkFrame(container, fg_color=Color.TRANSPARENT)
        self.btn_frame.pack(pady=35)

        # 使用你刚移植的高级 Button 组件
        self.btn_start = Button(
            self.btn_frame, text="开始抓取", width=160,
            fg_color=Color.BTN_SUCCESS_ALT, hover_color=Color.BTN_SUCCESS_ALT_HOVER, 
            command=self.start_scraping_thread
        )
        self.btn_start.pack(side="left", padx=20)

        self.btn_stop = Button(
            self.btn_frame, text="🛑 停止抓取", width=160,
            fg_color=Color.BTN_DANGER, hover_color=Color.BTN_DANGER_HOVER, 
            command=self.stop_scraping
        )
        self.btn_stop.configure(state="disabled") # 初始状态禁用
        self.btn_stop.pack(side="left", padx=20)

        # ================= 状态与进度条区 =================
        self.lbl_status = ctk.CTkLabel(container, text="等待分配任务...", text_color=Color.TEXT_MUTED, font=("Arial", 13))
        self.lbl_status.pack(pady=(10, 5))

        self.progress_bar = ctk.CTkProgressBar(container, width=500, height=12, corner_radius=6)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)
        self._on_source_changed(SOURCE_JACAR)

    def _on_source_changed(self, selected_source: str):
        if selected_source == SOURCE_MOFA:
            self.headless_checkbox.pack_forget()
            self.mofa_mode_frame.pack(pady=(2, 0), before=self.btn_frame)
            if hasattr(self, "btn_start"):
                self.btn_start.configure(text="执行 MOFA 任务")
            if hasattr(self, "lbl_status"):
                self.lbl_status.configure(
                    text="MOFA 默认仅扫描 1921—1927 卷册目录，不下载 PDF。",
                    text_color=Color.TEXT_MUTED,
                )
        else:
            self.mofa_mode_frame.pack_forget()
            self.headless_checkbox.pack(pady=(14, 0), before=self.btn_frame)
            if hasattr(self, "btn_start"):
                self.btn_start.configure(text="开始抓取")
            if hasattr(self, "lbl_status"):
                self.lbl_status.configure(text="等待分配任务...", text_color=Color.TEXT_MUTED)

    # ==========================================
    # ⚙️ 以下为你的核心打工人调度逻辑 (Worker Logic)
    # ==========================================
    def update_progress(self, current, total, message):
        def _update():
            self.lbl_status.configure(text=message)
            if total > 0:
                self.progress_bar.set(current / total)
        self.after(0, _update)

    def _set_scraper_controls_running(self, running: bool):
        state = "disabled" if running else "normal"
        self.source_menu.configure(state=state)
        self.mofa_mode_menu.configure(state=state)
        self.headless_checkbox.configure(state=state)
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def _open_monitor_window(self):
        if self.monitor_window is not None and self.monitor_window.winfo_exists():
            self.monitor_window.lift()
            self.monitor_window.focus_set()
            self._start_monitor_polling()
            return
        self.monitor_rows = {}
        self.monitor_window = ctk.CTkToplevel(self)
        self.monitor_window.title("下载任务监控")
        self.monitor_window.geometry("980x620")
        self.monitor_window.attributes("-topmost", True)
        self.monitor_window.protocol("WM_DELETE_WINDOW", self._close_monitor_window)

        self.monitor_title = ctk.CTkLabel(
            self.monitor_window,
            text="本次抓取下载监控",
            font=("Arial", 18, "bold"),
            text_color=Color.TEXT,
        )
        self.monitor_title.pack(pady=(14, 10))

        head = ctk.CTkLabel(
            self.monitor_window,
            text="状态说明：待下载 / 正在下载 / 已下载 / 已中止 / 失败",
            font=("Arial", 12),
            text_color=Color.TEXT_MUTED,
        )
        head.pack(pady=(0, 8))

        self.monitor_list_frame = ctk.CTkScrollableFrame(
            self.monitor_window,
            fg_color=("#f2f4f8", "#252932"),
            corner_radius=12,
            border_width=1,
            border_color=("#d7dde7", "#3c4452"),
            width=940,
            height=520,
        )
        self.monitor_list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self._bootstrap_monitor_run()
        self._start_monitor_polling()

    def _close_monitor_window(self):
        if self.monitor_poll_job is not None:
            try:
                self.after_cancel(self.monitor_poll_job)
            except Exception:
                pass
            self.monitor_poll_job = None
        if self.monitor_window is not None and self.monitor_window.winfo_exists():
            self.monitor_window.destroy()
        self.monitor_window = None
        self.monitor_list_frame = None
        self.monitor_title = None
        self.monitor_rows = {}

    def _bootstrap_monitor_run(self):
        if self.awaiting_run_id:
            if self.monitor_title:
                self.monitor_title.configure(text="下载任务监控（等待任务启动...）")
            return
        if self.current_run_id is None:
            self.current_run_id = self.db_service.get_latest_run_id(prefer_active=True)
        self._refresh_monitor_from_db()

    def _status_from_db(self, doc_status: str | None, event_type: str | None) -> str:
        s = (event_type or "").strip().lower()
        d = (doc_status or "").strip().lower()
        if s == "downloading" or d == "downloading":
            return "正在下载"
        if s == "queued" or d in {"discovered", "pending", "queued"}:
            return "待下载"
        if s == "failed" or d in {"failed", "error"}:
            return "失败"
        if s == "aborted":
            return "已中止"
        if s == "succeeded" or d in {"downloaded", "completed", "pending_hoover"}:
            return "已下载"
        return "待下载"

    @staticmethod
    def _format_event_message(raw_message: str | None) -> str:
        """
        统一展示事件消息：
        - 若形如 `CODE | 中文说明 | detail=...`，优先展示 `CODE` 与中文说明；
        - 否则按原始文本兜底。
        """
        text = str(raw_message or "").strip()
        if not text:
            return "0.00B/s"
        parts = [p.strip() for p in text.split("|")]
        if len(parts) >= 2 and parts[0].startswith(("E_", "A_")):
            msg = f"{parts[0]} {parts[1]}"
            if len(parts) >= 3 and parts[2]:
                msg += f" ({parts[2]})"
            return msg
        return text

    def _apply_monitor_state(self, task_id: str, title: str, status: str, speed_text: str, progress: float | None):
        self._ensure_monitor_row(task_id, title)
        row = self.monitor_rows.get(task_id)
        if not row:
            return
        color = Color.TEXT_MUTED
        if status == "正在下载":
            color = Color.PRIMARY
        elif status == "已下载":
            color = Color.TEXT_SUCCESS
        elif status in {"失败", "已中止"}:
            color = Color.RED
        row["status"].configure(text=status, text_color=color)
        row["speed"].configure(text=speed_text, text_color=Color.TEXT_MUTED)
        if progress is None:
            current = row["bar"].get()
            if status == "正在下载" and current < 0.95:
                row["bar"].set(min(0.95, current + 0.02))
        else:
            row["bar"].set(max(0.0, min(1.0, float(progress))))

    def _clear_monitor_rows(self):
        for item in list(self.monitor_rows.values()):
            row_widget = item.get("row")
            try:
                if row_widget is not None:
                    row_widget.destroy()
            except Exception:
                pass
        self.monitor_rows = {}

    def _refresh_monitor_from_db(self):
        if not self.monitor_window or not self.monitor_window.winfo_exists():
            return
        run_id = self.current_run_id
        if run_id is None:
            if self.monitor_title:
                self.monitor_title.configure(text="下载任务监控（暂无运行记录）")
            return

        summary = self.db_service.get_run_summary(run_id)
        if self.monitor_title:
            if summary:
                kw = summary.get("keyword") or "-"
                done = summary.get("completed") or 0
                total = summary.get("dispatched") or 0
                self.monitor_title.configure(text=f"下载任务监控 · Run #{run_id} · 关键词 {kw} · {done}/{total}")
            else:
                self.monitor_title.configure(text=f"下载任务监控 · Run #{run_id}")

        rows = self.db_service.get_run_monitor_rows(run_id)
        active_task_ids: set[str] = set()
        for item in rows:
            task_id = str(item.get("task_id") or "")
            if not task_id:
                continue
            active_task_ids.add(task_id)
            title = str(item.get("title") or task_id)
            status = self._status_from_db(item.get("doc_status"), item.get("event_type"))
            speed_text = "N/A"
            progress = None
            et = (item.get("event_type") or "").strip().lower()
            if status == "待下载":
                speed_text = "0.00B/s"
                progress = 0.0
            elif status == "已下载":
                speed_text = "完成"
                progress = 1.0
            elif status in {"失败", "已中止"}:
                speed_text = self._format_event_message(item.get("message"))
                progress = None
            elif et == "downloading":
                speed_text = "运行中"
                progress = None
            self._apply_monitor_state(task_id, title, status, speed_text, progress)

        # 清理不属于当前 run 的旧行，避免显示上一次任务残留
        stale_task_ids = [tid for tid in self.monitor_rows.keys() if tid not in active_task_ids]
        for tid in stale_task_ids:
            item = self.monitor_rows.pop(tid, None)
            if not item:
                continue
            row_widget = item.get("row")
            try:
                if row_widget is not None:
                    row_widget.destroy()
            except Exception:
                pass

    def _start_monitor_polling(self):
        if not self.monitor_window or not self.monitor_window.winfo_exists():
            return
        if self.monitor_poll_job is not None:
            try:
                self.after_cancel(self.monitor_poll_job)
            except Exception:
                pass
            self.monitor_poll_job = None

        def _tick():
            self._refresh_monitor_from_db()
            if self.monitor_window and self.monitor_window.winfo_exists():
                self.monitor_poll_job = self.after(1200, _tick)
            else:
                self.monitor_poll_job = None

        self.monitor_poll_job = self.after(100, _tick)

    def _ensure_monitor_row(self, task_id: str, title: str):
        if not self.monitor_list_frame:
            return
        if task_id in self.monitor_rows:
            return
        row = ctk.CTkFrame(self.monitor_list_frame, fg_color=("#e8edf5", "#2d3340"), corner_radius=10)
        row.pack(fill="x", padx=10, pady=8)

        name = ctk.CTkLabel(row, text=title, anchor="w", font=("Arial", 13, "bold"), text_color=Color.TEXT)
        name.pack(fill="x", padx=12, pady=(10, 2))

        meta = ctk.CTkFrame(row, fg_color=Color.TRANSPARENT)
        meta.pack(fill="x", padx=12, pady=(0, 4))
        status_lbl = ctk.CTkLabel(meta, text="待下载", font=("Arial", 12), text_color=Color.TEXT_MUTED)
        status_lbl.pack(side="left")
        speed_lbl = ctk.CTkLabel(meta, text="0.00B/s", font=("Arial", 12), text_color=Color.TEXT_MUTED)
        speed_lbl.pack(side="right")

        bar = ctk.CTkProgressBar(row, height=10, corner_radius=6)
        bar.set(0)
        bar.pack(fill="x", padx=12, pady=(2, 10))

        self.monitor_rows[task_id] = {
            "row": row,
            "status": status_lbl,
            "speed": speed_lbl,
            "bar": bar,
        }

    def on_task_enqueued(self, task: dict):
        # 阶段 4 起监控由 DB 驱动，此回调保留为兼容空实现
        return

    def on_task_update(self, task_id: str, payload: dict):
        # 阶段 4 起监控由 DB 驱动，此回调保留为兼容空实现
        return

    def on_run_started(self, run_id: int):
        def _ui():
            new_run_id = int(run_id)
            self.awaiting_run_id = False
            if self.current_run_id != new_run_id:
                self._clear_monitor_rows()
            self.current_run_id = new_run_id
            if self.monitor_window and self.monitor_window.winfo_exists():
                self._refresh_monitor_from_db()
                self._start_monitor_polling()
        self.after(0, _ui)

    def finish_scraping(self, message="🎉 任务圆满完成！所有文件已下载。"):
        def _finish():
            self.lbl_status.configure(text=message, text_color=Color.TEXT_SUCCESS)
            self._set_scraper_controls_running(False)
            if self.monitor_window and self.monitor_window.winfo_exists():
                self.monitor_window.attributes("-topmost", False)
            messagebox.showinfo("提示", message)
        self.after(0, _finish)

    def finish_scraping_cancelled(self, message: str):
        def _finish():
            self.lbl_status.configure(text=message, text_color=Color.TEXT_WARNING)
            self._set_scraper_controls_running(False)
            if self.monitor_window and self.monitor_window.winfo_exists():
                self.monitor_window.attributes("-topmost", False)
            messagebox.showinfo("任务已中止", message)
        self.after(0, _finish)

    def finish_scraping_error(self, message: str):
        def _finish():
            self.lbl_status.configure(text=message, text_color=Color.RED)
            self._set_scraper_controls_running(False)
            if self.monitor_window and self.monitor_window.winfo_exists():
                self.monitor_window.attributes("-topmost", False)
            messagebox.showerror("任务失败", message)
        self.after(0, _finish)

    def _show_mofa_catalog_result(self, result: MofaWorkflowResult):
        def _show():
            if self.mofa_result_window is not None and self.mofa_result_window.winfo_exists():
                self.mofa_result_window.destroy()
            self.mofa_result_window = ctk.CTkToplevel(self)
            self.mofa_result_window.title("MOFA 目录扫描结果")
            self.mofa_result_window.geometry("980x680")

            content_count = len(result.content_items)
            summary = (
                f"{result.year_from}—{result.year_to} · 卷册 {len(result.volumes)} · "
                f"PDF {len(result.all_items)} · 正文 {content_count} · "
                f"标题命中 {len(result.matched_items)} · "
                f"待下载 {len(result.selected_items)}"
            )
            ctk.CTkLabel(
                self.mofa_result_window,
                text="MOFA 《日本外交文書》目录结果",
                font=("Arial", 20, "bold"),
                text_color=Color.TEXT,
            ).pack(pady=(16, 5))
            ctk.CTkLabel(
                self.mofa_result_window,
                text=summary,
                font=("Arial", 13),
                text_color=Color.TEXT_MUTED,
            ).pack(pady=(0, 10))

            display_items = result.matched_items if result.keyword and result.matched_items else result.all_items
            matched_urls = {item.pdf_url for item in result.matched_items}
            textbox = ctk.CTkTextbox(
                self.mofa_result_window,
                width=930,
                height=570,
                font=("Menlo", 12),
                wrap="word",
            )
            textbox.pack(fill="both", expand=True, padx=18, pady=(0, 16))
            lines = []
            for index, item in enumerate(display_items, start=1):
                marker = "命中" if item.pdf_url in matched_urls else item.item_kind
                lines.append(
                    f"{index:04d}  [{marker}]  {item.volume.gregorian_year}  "
                    f"{item.volume.volume_label}  |  {item.title}\n{item.pdf_url}\n"
                )
            textbox.insert("1.0", "\n".join(lines) if lines else "当前范围内没有可展示的目录条目。")
            textbox.configure(state="disabled")
        self.after(0, _show)

    @staticmethod
    def _mofa_finish_message(result: MofaWorkflowResult) -> str:
        if result.aborted:
            return (
                f"MOFA 任务已中止。已扫描 {len(result.volumes)} 个卷册，"
                f"新下载 {result.downloaded} 份，跳过 {result.skipped} 份。"
            )
        if result.mode == MOFA_MODE_SCAN:
            return (
                f"MOFA 目录扫描完成：{len(result.volumes)} 个卷册，"
                f"{len(result.all_items)} 个 PDF，其中正文 {len(result.content_items)} 个，"
                f"标题命中 {len(result.matched_items)} 个。未下载 PDF。"
            )
        return (
            f"MOFA 下载任务完成：选中 {len(result.selected_items)} 份，"
            f"新下载 {result.downloaded} 份，已存在/修复 {result.skipped} 份，"
            f"失败 {result.failed} 份。"
        )

    def _run_mofa_workflow(self, keyword: str, start_year: int, end_year: int, mode: str):
        try:
            service = MofaWorkflowService(db_service=self.db_service)
            result = service.run(
                keyword=keyword,
                year_from=start_year,
                year_to=end_year,
                mode=mode,
                stop_event=self.stop_event,
                on_progress=self.update_progress,
                on_run_started=self.on_run_started,
                on_catalog_ready=self._show_mofa_catalog_result,
            )
            if result.aborted:
                self.finish_scraping_cancelled(self._mofa_finish_message(result))
            else:
                self.finish_scraping(self._mofa_finish_message(result))
        except Exception as exc:
            self.finish_scraping_error(f"MOFA 任务失败：{exc}")

    def stop_scraping(self):
        self.stop_event.set()
        self.lbl_status.configure(text="🛑 收到停止指令，正在等待所有线程安全退出...", text_color=Color.TEXT_WARNING)
        self.btn_stop.configure(state="disabled")

    def start_scraping_thread(self):
        # 💡 注意这里：咱们使用了高级 Input 组件独有的 getValue() 方法！
        kw = self.entry_keyword.getValue().strip()
        sy = self.entry_start_year.getValue().strip()
        ey = self.entry_end_year.getValue().strip()

        selected_source = self.source_var.get()
        mofa_mode = MOFA_MODE_LABELS.get(self.mofa_mode_var.get(), MOFA_MODE_SCAN)

        if not kw and not (selected_source == SOURCE_MOFA and mofa_mode == MOFA_MODE_SCAN):
            messagebox.showwarning("提示", "检索关键词不能为空哦！")
            return

        if selected_source == SOURCE_MOFA:
            try:
                start_year = int(sy)
                end_year = int(ey)
                if start_year > end_year or start_year < 1921 or end_year > 1927:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("提示", "MOFA 年份范围请输入 1921—1927，且起始年不得晚于结束年。")
                return

        self.stop_event.clear()
        self._set_scraper_controls_running(True)
        self.lbl_status.configure(
            text=(
                "正在读取 MOFA 卷册目录..."
                if selected_source == SOURCE_MOFA
                else "🚀 正在启动浏览器并连接数据库..."
            ),
            text_color=Color.PRIMARY,
        )
        self.progress_bar.set(0)
        self.awaiting_run_id = True
        self.current_run_id = None
        if selected_source != SOURCE_MOFA or mofa_mode != MOFA_MODE_SCAN:
            self._open_monitor_window()

        if selected_source == SOURCE_MOFA:
            scraper_thread = threading.Thread(
                target=self._run_mofa_workflow,
                args=(kw, start_year, end_year, mofa_mode),
                daemon=True,
            )
            scraper_thread.start()
            return

        # 启动后台爬虫线程
        scraper_thread = threading.Thread(
            target=core_scraper.jacar_auto_search,
            args=(kw, sy, ey, self.update_progress, self.finish_scraping, self.stop_event),
            kwargs={
                "headless": bool(self.headless_var.get()),
                "on_task_enqueued": self.on_task_enqueued,
                "on_task_update": self.on_task_update,
                "on_run_started": self.on_run_started,
            }
        )
        scraper_thread.daemon = True
        scraper_thread.start()
