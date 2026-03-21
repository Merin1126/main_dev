from screens.scraper_screen import ScraperScreen
from screens.ocr_screen import OCRScreen  # <-- 新增这行！
from screens.setting_screen import SettingScreen
import customtkinter as ctk


# ==========================================
# 🚧 临时占位页面 (等咱们把核心 GUI 搬过来就替换掉)
# ==========================================
class DummyScraperScreen(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        ctk.CTkLabel(self, text="🚀 史料高并发抓取页面 (开发中...)", font=("Arial", 24, "bold"), text_color="#28a745").pack(expand=True)

class DummyOCRScreen(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        ctk.CTkLabel(self, text="👁️ 史料 OCR 校对页面 (开发中...)", font=("Arial", 24, "bold"), text_color="#1F6AA5").pack(expand=True)

# ==========================================
# 👑 屏幕大管家 (Screen Manager)
# ==========================================
class ScreenManager(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        # 设置透明背景让它融入主窗口
        super().__init__(master, fg_color="transparent", corner_radius=0, **kwargs)
        self.master = master
        
        # 初始化所有子页面
        self.initialize_screens()
        
        # 默认渲染抓取页面
        self.render("scraper")

    def initialize_screens(self) -> None:
        """初始化并把所有页面实例存入内存"""
        self.scraper_screen = ScraperScreen(self)
        self.ocr_screen = OCRScreen(self)  # <-- 换成真身！
        self.setting_screen = SettingScreen(self)

        self.screens = {
            "scraper": self.scraper_screen,
            "ocr": self.ocr_screen,
            "setting": self.setting_screen
    }

    def render(self, screen_name: str) -> None:
        """
        核心路由逻辑：把选中的页面 pack 出来，把其他的隐藏掉
        """
        for name, screen_obj in self.screens.items():
            if screen_name == name:
                # 显示目标页面
                screen_obj.pack(expand=True, fill="both")
            else:
                # 隐藏非目标页面
                screen_obj.pack_forget()

    def change_screen(self, screen_name: str) -> None:
        """对外暴露的切换页面方法"""
        
        # 💡 新增逻辑：如果目标是 OCR 界面，强制刷新一次左侧列表
        if screen_name == "ocr":
            # 重新呼叫 OCR 界面里的列表加载“打工人函数” (Refresh Handler)
            self.ocr_screen._load_file_list()
            
        # 执行原本的渲染切换逻辑
        self.render(screen_name)