import threading
import customtkinter as ctk
from tkinter import messagebox

# 导入咱们刚移植过来的高级 UI 组件
from components.ui.button import Button
from components.ui.input import Input

# 导入核心爬虫打工人
import core_scraper

class ScraperScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        # 继承大管家的透明背景
        super().__init__(master, fg_color="transparent", **kwargs)
        self.master = master
        self.stop_event = threading.Event()

        self._setup_ui()

    def _setup_ui(self):
        # 页面大标题
        title_lbl = ctk.CTkLabel(self, text="🚀 史料高并发抓取控制台", font=("Arial", 28, "bold"))
        title_lbl.pack(pady=(30, 10))

        # 中间的圆角主容器 (Main Card Container)
        container = ctk.CTkFrame(self, corner_radius=15)
        container.pack(pady=20, padx=40, fill="both", expand=True)

        # ================= 表单输入区 =================
        ctk.CTkLabel(container, text="🔍 检索关键词:", font=("Arial", 15, "bold")).pack(pady=(30, 5))
        # 使用你刚移植的高级 Input 组件
        self.entry_keyword = Input(container, width=400, defaultValue="反帝國主義")
        self.entry_keyword.pack()

        ctk.CTkLabel(container, text="📅 起始年份 (如 1921):", font=("Arial", 15, "bold")).pack(pady=(20, 5))
        self.entry_start_year = Input(container, width=400, defaultValue="1921")
        self.entry_start_year.pack()

        ctk.CTkLabel(container, text="📅 结束年份 (如 1927):", font=("Arial", 15, "bold")).pack(pady=(20, 5))
        self.entry_end_year = Input(container, width=400, defaultValue="1927")
        self.entry_end_year.pack()

        # ================= 操作按钮区 =================
        btn_frame = ctk.CTkFrame(container, fg_color="transparent")
        btn_frame.pack(pady=35)

        # 使用你刚移植的高级 Button 组件
        self.btn_start = Button(
            btn_frame, text="🚀 开始抓取", width=160, 
            fg_color="#28a745", hover_color="#218838", 
            command=self.start_scraping_thread
        )
        self.btn_start.pack(side="left", padx=20)

        self.btn_stop = Button(
            btn_frame, text="🛑 停止抓取", width=160, 
            fg_color="#dc3545", hover_color="#c82333", 
            command=self.stop_scraping
        )
        self.btn_stop.configure(state="disabled") # 初始状态禁用
        self.btn_stop.pack(side="left", padx=20)

        # ================= 状态与进度条区 =================
        self.lbl_status = ctk.CTkLabel(container, text="等待分配任务...", text_color="gray", font=("Arial", 13))
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

    def finish_scraping(self, message="🎉 任务圆满完成！所有文件已下载。"):
        def _finish():
            self.lbl_status.configure(text=message, text_color="green")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            messagebox.showinfo("提示", message)
        self.after(0, _finish)

    def stop_scraping(self):
        self.stop_event.set()
        self.lbl_status.configure(text="🛑 收到停止指令，正在等待所有线程安全退出...", text_color="orange")
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
        self.lbl_status.configure(text="🚀 正在启动浏览器并连接数据库...", text_color="#1F6AA5")
        self.progress_bar.set(0)

        # 启动后台爬虫线程
        scraper_thread = threading.Thread(
            target=core_scraper.jacar_auto_search,
            args=(kw, sy, ey, self.update_progress, self.finish_scraping, self.stop_event)
        )
        scraper_thread.daemon = True
        scraper_thread.start()