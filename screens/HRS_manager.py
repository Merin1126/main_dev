from screens.scraper_screen import ScraperScreen
from screens.ocr_screen import OCRScreen
from screens.translation_screen import TranslationScreen
from screens.analysis_screen import AnalysisScreen
from screens.setting_screen import SettingScreen
import customtkinter as ctk
from config.settings import Color


_DOC_ROUTES = frozenset({"ocr", "translation", "analysis"})


class ScreenManager(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT, corner_radius=0, **kwargs)
        self.master = master

        self.initialize_screens()

        self.render("scraper")

    def initialize_screens(self) -> None:
        """初始化并把所有页面实例存入内存（各文档页状态彼此独立）。"""
        self.scraper_screen = ScraperScreen(self)
        self.ocr_screen = OCRScreen(self)
        self.translation_screen = TranslationScreen(self)
        self.analysis_screen = AnalysisScreen(self)
        self.setting_screen = SettingScreen(self)

        self.screens = {
            "scraper": self.scraper_screen,
            "ocr": self.ocr_screen,
            "translation": self.translation_screen,
            "analysis": self.analysis_screen,
            "setting": self.setting_screen,
        }

    def render(self, screen_name: str) -> None:
        for name, screen_obj in self.screens.items():
            if screen_name == name:
                screen_obj.pack(expand=True, fill="both")
            else:
                screen_obj.pack_forget()

    def change_screen(self, screen_name: str) -> None:
        if screen_name in _DOC_ROUTES:
            self.screens[screen_name]._load_file_list()

        self.render(screen_name)
