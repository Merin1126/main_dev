import threading
import os
import customtkinter as ctk
from tkinter import messagebox
from config.settings import Color

# 导入咱们刚移植过来的高级 UI 组件
from components.ui.button import Button
from components.ui.input import Input

# 导入核心爬虫打工人
import core_scraper

class ScraperScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        # 继承大管家的透明背景
        super().__init__(master, fg_color=("#f5f6f8", "#1f1f23"), **kwargs)
        self.master = master
        self.stop_event = threading.Event()
        self.monitor_window = None
        self.monitor_rows: dict[str, dict] = {}
        self.monitor_list_frame = None

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
        container = ctk.CTkFrame(
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

        self.headless_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            container,
            text="无头模式（不打开浏览器窗口）",
            variable=self.headless_var,
            font=("Arial", 13),
        ).pack(pady=(14, 0))

        # ================= 操作按钮区 =================
        btn_frame = ctk.CTkFrame(container, fg_color=Color.TRANSPARENT)
        btn_frame.pack(pady=35)

        # 使用你刚移植的高级 Button 组件
        self.btn_start = Button(
            btn_frame, text="开始抓取", width=160,
            fg_color=Color.BTN_SUCCESS_ALT, hover_color=Color.BTN_SUCCESS_ALT_HOVER, 
            command=self.start_scraping_thread
        )
        self.btn_start.pack(side="left", padx=20)

        self.btn_stop = Button(
            btn_frame, text="🛑 停止抓取", width=160, 
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

    # ==========================================
    # ⚙️ 以下为你的核心打工人调度逻辑 (Worker Logic)
    # ==========================================
    def update_progress(self, current, total, message):
        def _update():
            self.lbl_status.configure(text=message)
            if total > 0:
                self.progress_bar.set(current / total)
        self.after(0, _update)

    def _open_monitor_window(self):
        if self.monitor_window is not None and self.monitor_window.winfo_exists():
            self.monitor_window.lift()
            self.monitor_window.focus_set()
            return
        self.monitor_rows = {}
        self.monitor_window = ctk.CTkToplevel(self)
        self.monitor_window.title("下载任务监控")
        self.monitor_window.geometry("980x620")
        self.monitor_window.attributes("-topmost", True)

        title = ctk.CTkLabel(
            self.monitor_window,
            text="本次抓取下载监控",
            font=("Arial", 18, "bold"),
            text_color=Color.TEXT,
        )
        title.pack(pady=(14, 10))

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
        def _ui():
            if not self.monitor_window or not self.monitor_window.winfo_exists():
                self._open_monitor_window()
            task_id = task.get("task_id") or task.get("save_path") or ""
            if not task_id:
                return
            title = task.get("title") or os.path.basename(task.get("save_path") or task_id)
            self._ensure_monitor_row(task_id, title)
            row = self.monitor_rows.get(task_id)
            if row:
                status = task.get("status", "待下载")
                color = Color.TEXT_MUTED
                if status == "正在下载":
                    color = Color.PRIMARY
                elif status == "已下载":
                    color = Color.TEXT_SUCCESS
                elif status in {"失败", "已中止"}:
                    color = Color.RED
                row["status"].configure(text=status, text_color=color)
                row["speed"].configure(text=str(task.get("speed_text", "0.00B/s")), text_color=Color.TEXT_MUTED)
                progress = task.get("progress")
                if progress is None:
                    row["bar"].set(0)
                else:
                    row["bar"].set(max(0.0, min(1.0, float(progress))))
        self.after(0, _ui)

    def on_task_update(self, task_id: str, payload: dict):
        def _ui():
            if not task_id:
                return
            if task_id not in self.monitor_rows:
                # 兜底：回调先到时创建占位行
                self._ensure_monitor_row(task_id, os.path.basename(task_id))
            row = self.monitor_rows.get(task_id)
            if not row:
                return
            status = payload.get("status")
            progress = payload.get("progress")
            speed_text = payload.get("speed_text")
            if status:
                color = Color.TEXT_MUTED
                if status == "正在下载":
                    color = Color.PRIMARY
                elif status == "已下载":
                    color = Color.TEXT_SUCCESS
                elif status in {"失败", "已中止"}:
                    color = Color.RED
                row["status"].configure(text=status, text_color=color)
            if speed_text is not None:
                row["speed"].configure(text=str(speed_text), text_color=Color.TEXT_MUTED)
            if progress is None:
                # 未知总量时保持动画感：不倒退进度
                current = row["bar"].get()
                if status == "正在下载" and current < 0.95:
                    row["bar"].set(min(0.95, current + 0.02))
            else:
                p = max(0.0, min(1.0, float(progress)))
                row["bar"].set(p)
        self.after(0, _ui)

    def finish_scraping(self, message="🎉 任务圆满完成！所有文件已下载。"):
        def _finish():
            self.lbl_status.configure(text=message, text_color=Color.TEXT_SUCCESS)
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            if self.monitor_window and self.monitor_window.winfo_exists():
                self.monitor_window.attributes("-topmost", False)
            messagebox.showinfo("提示", message)
        self.after(0, _finish)

    def stop_scraping(self):
        self.stop_event.set()
        self.lbl_status.configure(text="🛑 收到停止指令，正在等待所有线程安全退出...", text_color=Color.TEXT_WARNING)
        self.btn_stop.configure(state="disabled")

    def start_scraping_thread(self):
        # 💡 注意这里：咱们使用了高级 Input 组件独有的 getValue() 方法！
        kw = self.entry_keyword.getValue().strip()
        sy = self.entry_start_year.getValue().strip()
        ey = self.entry_end_year.getValue().strip()

        if not kw:
            messagebox.showwarning("提示", "检索关键词不能为空哦！")
            return

        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="🚀 正在启动浏览器并连接数据库...", text_color=Color.PRIMARY)
        self.progress_bar.set(0)
        self._open_monitor_window()

        # 启动后台爬虫线程
        scraper_thread = threading.Thread(
            target=core_scraper.jacar_auto_search,
            args=(kw, sy, ey, self.update_progress, self.finish_scraping, self.stop_event),
            kwargs={
                "headless": bool(self.headless_var.get()),
                "on_task_enqueued": self.on_task_enqueued,
                "on_task_update": self.on_task_update,
            }
        )
        scraper_thread.daemon = True
        scraper_thread.start()