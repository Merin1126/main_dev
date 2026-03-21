from components.HRS_navigation import Navigation
import customtkinter as ctk
# from components.navigation import Navigation
from screens.HRS_manager import ScreenManager

class HRSApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        # 1. 基础窗口设置
        self.width = 1260
        self.height = 700
        self.title("HRS 史料全自动采集器 V2.2")
        self.minsize(800, 600)
        self._center_window() # 调用原作者优雅的居中算法

        # 全局外观设置
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        # ==========================================
        # 🎭 舞台布置区 (Layout)
        # ==========================================
        
        # 左侧：导航栏 (Navigation Bar) - 正式接入！
        self.navigation = Navigation(self)
        self.navigation.pack(side="left", fill="y")

        # 右侧：屏幕大管家 (Screen Manager)
        self.screen_manager = ScreenManager(self)
        self.screen_manager.pack(side="right", fill="both", expand=True)

    def _center_window(self) -> None:
        """
        核心居中算法 (Center Window Math)
        获取当前屏幕分辨率，计算出完美的 X 和 Y 坐标让窗口居中
        """
        self.update_idletasks()
        x = (self.winfo_screenwidth() // 2) - (self.width // 2)
        y = (self.winfo_screenheight() // 2) - (self.height // 2)
        self.geometry(f"{self.width}x{self.height}+{x}+{y}")

    def navigate(self, screen_name: str) -> None:
        """
        视图路由中心 (Router Entry)
        左边导航栏被点击时，会调用这个函数，由它去命令大管家换页面
        """
        print(f"👉 收到路由请求，准备切换至页面: {screen_name}")
        # 等咱们接入大管家后，这里将激活：
        # self.screen_manager.navigate(screen_name)


if __name__ == "__main__":
    app = HRSApp()
    app.mainloop()