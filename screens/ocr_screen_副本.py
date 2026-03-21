import os
import sys
import subprocess
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import fitz  # PyMuPDF
from PIL import Image, ImageTk
from docx import Document

# 导入咱们的高级 UI 组件 (确保路径正确)
from components.ui.button import Button

class OCRScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.master = master
        
        # 自动定位下载文件夹
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.download_dir = os.path.join(base_dir, "JACAR_Downloads")
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)
            
        # 状态存储盒子 (Variables)
        self.current_pdf = None
        self.current_page = 0
        self.zoom_factor = 1.0  # 初始缩放比例
        self.tk_image = None
        self.current_image_item = None
        self.pdf_files = [] # 存储完整路径的列表
        
        self._setup_ui()
        self._load_file_list()

    def _setup_ui(self):
        # 【核心改动 1】：移除原先写死的 grid_columnconfigure 布局
        
        # 【核心改动 2】：引入原生的分屏画板容器 (PanedWindow)
        # 负责管理可拖拽的分割线 (Sash)
        self.paned_window = tk.PanedWindow(
            self, 
            orient="horizontal", 
            bg="#1a1a1a",              # 背景色：设置深灰以匹配暗色主题，这也将是分割线的颜色
            sashwidth=8,               # 分割线宽度：8 个像素，既不突兀又容易用鼠标抓住
            sashrelief="flat",         # 分割线样式：扁平化，更有现代感
            sashcursor="sb_h_double_arrow", # 鼠标变成左右拖拽的双箭头图标
            borderwidth=0
        )
        self.paned_window.pack(fill="both", expand=True, padx=5, pady=5)

        # ================= 左侧：文件列表区 =================
        self.left_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        # 【核心改动 3】：使用 add() 加入容器，并设置 minsize 防止被拉得过窄
        self.paned_window.add(self.left_frame, minsize=150, stretch="always")
        
        ctk.CTkLabel(self.left_frame, text="📁 史料文件库", font=("Arial", 16, "bold")).pack(pady=10)
        
        list_container = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        list_container.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.v_scrollbar = ctk.CTkScrollbar(list_container, orientation="vertical")
        self.h_scrollbar = ctk.CTkScrollbar(list_container, orientation="horizontal")
        
        self.file_listbox = tk.Listbox(
            list_container, 
            yscrollcommand=self.v_scrollbar.set,
            xscrollcommand=self.h_scrollbar.set,
            bg="#2b2b2b", fg="white", selectbackground="#1F6AA5",
            font=("Arial", 12), borderwidth=0, highlightthickness=0
        )
        
        self.v_scrollbar.configure(command=self.file_listbox.yview)
        self.h_scrollbar.configure(command=self.file_listbox.xview)
        
        self.v_scrollbar.pack(side="right", fill="y")
        self.h_scrollbar.pack(side="bottom", fill="x")
        self.file_listbox.pack(side="left", fill="both", expand=True)
        self.file_listbox.bind('<<ListboxSelect>>', self.on_file_select)

        list_action_frame = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        list_action_frame.pack(fill="x", padx=8, pady=(0, 10))

        Button(
            list_action_frame,
            text="打开史料文件库",
            height=38,
            command=self.open_download_folder
        ).pack(fill="x", pady=(0, 6))

        Button(
            list_action_frame,
            text="刷新列表",
            height=38,
            command=self.refresh_file_list
        ).pack(fill="x")

        # ================= 中间：PDF 阅读器 =================
        self.mid_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        # PDF 区域是视觉重点，最小宽度给大一点
        self.paned_window.add(self.mid_frame, minsize=400, stretch="always")
        
        toolbar = ctk.CTkFrame(self.mid_frame, fg_color="transparent")
        toolbar.pack(fill="x", pady=5, padx=5)
        
        Button(toolbar, text="➖ 缩小", width=60, height=30, command=self.zoom_out).pack(side="left", padx=5)
        Button(toolbar, text="➕ 放大", width=60, height=30, command=self.zoom_in).pack(side="left", padx=5)
        
        self.page_label = ctk.CTkLabel(toolbar, text="页码: 0 / 0", font=("Arial", 13, "bold"))
        self.page_label.pack(side="left", expand=True)
        
        Button(toolbar, text="◀ 上一页", width=80, height=30, command=self.prev_page).pack(side="left", padx=5)
        Button(toolbar, text="▶ 下一页", width=80, height=30, command=self.next_page).pack(side="left", padx=5)
        
        self.canvas = tk.Canvas(self.mid_frame, bg="#2b2b2b", highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag_motion)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)

        # ================= 右侧：OCR 编辑与导出 =================
        self.right_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        self.paned_window.add(self.right_frame, minsize=200, stretch="always")
        
        ctk.CTkLabel(self.right_frame, text="📝 OCR 文字校对区", font=("Arial", 16, "bold")).pack(pady=10)
        
        self.text_editor = ctk.CTkTextbox(self.right_frame, wrap="word", font=("Arial", 14), corner_radius=8)
        self.text_editor.pack(fill="both", expand=True, padx=10, pady=10)
        self.text_editor.insert("0.0", "👈 请在左侧选择一份已下载的史料 PDF 文件。\n\n此处将显示 Google API 提取的文本...")
        
        self.btn_export = Button(
            self.right_frame, text="💾 确认并导出文档", 
            fg_color="#1F6AA5", hover_color="#144870",
            width=200, height=45, command=self.export_document
        )
        self.btn_export.pack(pady=15, padx=10, fill="x")

    def _load_file_list(self):
        """扫描目录并生成带有序号和缩进的左侧列表项"""
        self.file_listbox.delete(0, tk.END)
        self.pdf_files.clear()
        
        if not os.path.exists(self.download_dir): return
            
        # 1. 先把所有 PDF 文件挑出来，并按首字母排个序（让列表更整洁）
        pdf_list = [f for f in os.listdir(self.download_dir) if f.lower().endswith('.pdf')]
        pdf_list.sort()
        
        # 2. 使用 enumerate (枚举器) 自动生成序号，start=1 表示从 1 开始数
        for index, filename in enumerate(pdf_list, start=1):
            file_path = os.path.join(self.download_dir, filename)
            self.pdf_files.append(file_path)
            
            # 💡 核心魔法：使用 f-string 格式化字符串
            # "   " 提供左侧安全距离 (留白)
            # {index}. 提供自动序号
            display_text = f"   {index}. {filename}"
            
            # 把组合好的优美文本插入列表
            self.file_listbox.insert(tk.END, display_text)

    def refresh_file_list(self):
        """手动刷新左侧文件列表"""
        self._load_file_list()
        messagebox.showinfo("提示", "史料文件库列表已刷新。")

    def open_download_folder(self):
        """在系统文件管理器中打开史料文件库目录"""
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

        try:
            if sys.platform.startswith("darwin"):
                subprocess.run(["open", self.download_dir], check=True)
            elif os.name == "nt":
                os.startfile(self.download_dir)
            else:
                subprocess.run(["xdg-open", self.download_dir], check=True)
        except Exception as e:
            messagebox.showerror("错误", f"无法打开文件夹:\n{e}")
            
    def on_file_select(self, event):
        """处理列表点击事件"""
        selection = self.file_listbox.curselection()
        if selection:
            index = selection[0]
            file_path = self.pdf_files[index]
            self.open_pdf(file_path)

    def open_pdf(self, file_path):
        if self.current_pdf:
            self.current_pdf.close()
        try:
            self.current_pdf = fitz.open(file_path)
            self.current_page = 0
            self.zoom_factor = 1.0
            self.render_page()
            
            self.text_editor.delete("0.0", "end")
            self.text_editor.insert("0.0", f"正在调用 Google API 提取 {os.path.basename(file_path)} 的数据...\n\n[模拟 OCR 结果...]")
        except Exception as e:
            messagebox.showerror("错误", f"无法打开 PDF 文件: {e}")

    def render_page(self):
        """渲染高清 PDF 页面到 Canvas"""
        if not self.current_pdf: return
        page = self.current_pdf[self.current_page]
        self.page_label.configure(text=f"页码: {self.current_page + 1} / {len(self.current_pdf)}")
        
        mat = fitz.Matrix(self.zoom_factor, self.zoom_factor)
        pix = page.get_pixmap(matrix=mat)
        
        mode = "RGBA" if pix.alpha else "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        self.tk_image = ImageTk.PhotoImage(img)
        
        self.canvas.delete("all")
        # 居中显示
        self.canvas.update_idletasks() # 确保能获取到准确的 canvas 宽高
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        self.current_image_item = self.canvas.create_image(cx, cy, anchor="center", image=self.tk_image)

    # --- 交互事件打工人 (Event Handlers) ---
    def on_drag_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def on_drag_motion(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def on_mouse_wheel(self, event):
        if event.delta > 0:
            self.zoom_in()
        else:
            self.zoom_out()

    def zoom_in(self):
        self.zoom_factor = min(5.0, self.zoom_factor + 0.2)
        self.render_page()

    def zoom_out(self):
        self.zoom_factor = max(0.4, self.zoom_factor - 0.2)
        self.render_page()

    def prev_page(self):
        if self.current_pdf and self.current_page > 0:
            self.current_page -= 1
            self.render_page()

    def next_page(self):
        if self.current_pdf and self.current_page < len(self.current_pdf) - 1:
            self.current_page += 1
            self.render_page()

    def export_document(self):
        # ... (与你原代码保持一致) ...
        text_content = self.text_editor.get("0.0", "end").strip()
        if not text_content:
            messagebox.showwarning("提示", "导出内容为空！")
            return
            
        file_path = filedialog.asksaveasfilename(
            defaultextension=".docx",
            filetypes=[("Word 文档", "*.docx"), ("Markdown 文件", "*.md")],
            title="保存提取的文字"
        )
        if not file_path: return
            
        try:
            if file_path.endswith('.md'):
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(text_content)
            elif file_path.endswith('.docx'):
                doc = Document()
                doc.add_paragraph(text_content)
                doc.save(file_path)
            messagebox.showinfo("成功", f"文件已成功保存至:\n{file_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"保存出错:\n{e}")