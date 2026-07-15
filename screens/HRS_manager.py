from screens.scraper_screen import ScraperScreen
from screens.ocr_screen import OCRScreen
from screens.translation_screen import TranslationScreen
from screens.analysis_screen import AnalysisScreen
from screens.setting_screen import SettingScreen
from screens.mofa_library_screen import MofaLibraryScreen
from screens.mofa_search_screen import MofaSearchScreen
from screens.mofa_candidate_screen import MofaCandidateScreen
from components.file_tree_sidebar import FileTreeSidebar
import customtkinter as ctk
import tkinter as tk
from config.settings import Color


_DOC_ROUTES = frozenset({"ocr", "translation", "analysis"})


class ScreenManager(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT, corner_radius=0, **kwargs)
        self.master = master

        self._sidebar_default_width = FileTreeSidebar.DEFAULT_WIDTH
        self._sidebar_attached = True

        self.main_paned = tk.PanedWindow(
            self,
            orient="horizontal",
            bg=self._resolve_paned_bg(),
            sashwidth=8,
            sashrelief="flat",
            sashcursor="sb_h_double_arrow",
            borderwidth=0,
        )
        self.main_paned.pack(fill="both", expand=True)

        self.sidebar = FileTreeSidebar(self.main_paned, width=self._sidebar_default_width)
        self.content_frame = ctk.CTkFrame(self.main_paned, fg_color=Color.TRANSPARENT, corner_radius=0)
        self.main_paned.add(self.sidebar, minsize=200, stretch="never")
        self.main_paned.add(self.content_frame, minsize=680, stretch="always")

        self.initialize_screens()

        self._hide_sidebar(initial=True)
        self.render("scraper")
        self.refresh_theme()

    def initialize_screens(self) -> None:
        """初始化并把所有页面实例存入内存（各文档页状态彼此独立）。"""
        self.scraper_screen = ScraperScreen(self.content_frame)
        self.ocr_screen = OCRScreen(self.content_frame)
        self.translation_screen = TranslationScreen(self.content_frame)
        self.analysis_screen = AnalysisScreen(self.content_frame)
        self.setting_screen = SettingScreen(self.content_frame)
        self.mofa_library_screen = MofaLibraryScreen(self.content_frame)
        self.mofa_search_screen = MofaSearchScreen(self.content_frame)
        self.mofa_candidate_screen = MofaCandidateScreen(self.content_frame)

        self.screens = {
            "scraper": self.scraper_screen,
            "ocr": self.ocr_screen,
            "translation": self.translation_screen,
            "analysis": self.analysis_screen,
            "setting": self.setting_screen,
            "mofa_library": self.mofa_library_screen,
            "mofa_search": self.mofa_search_screen,
            "mofa_candidates": self.mofa_candidate_screen,
        }

    def render(self, screen_name: str) -> None:
        for name, screen_obj in self.screens.items():
            if screen_name == name:
                screen_obj.pack(expand=True, fill="both")
                on_show = getattr(screen_obj, "on_show", None)
                if callable(on_show):
                    on_show()
            else:
                screen_obj.pack_forget()

    def change_screen(self, screen_name: str) -> None:
        if screen_name in _DOC_ROUTES:
            self._show_sidebar()
        else:
            self._hide_sidebar()

        self.render(screen_name)

    def _show_sidebar(self) -> None:
        if not self._sidebar_attached:
            self.main_paned.forget(self.content_frame)
            self.main_paned.add(self.sidebar, minsize=200, stretch="never")
            self.main_paned.add(self.content_frame, minsize=680, stretch="always")
            self._sidebar_attached = True
        self.sidebar.configure(width=self._sidebar_default_width)
        self.sidebar.pack_propagate(False)
        self.after(30, lambda: self._place_sash(self._sidebar_default_width))

    def _hide_sidebar(self, initial: bool = False) -> None:
        if self._sidebar_attached:
            self.main_paned.forget(self.sidebar)
            self._sidebar_attached = False
        # 初始阶段仍保留一次延迟刷新，避免个别平台首帧闪动
        if initial:
            self.after(20, self.main_paned.update_idletasks)

    def _place_sash(self, x: int) -> None:
        if not self.winfo_exists():
            return
        total_width = self.main_paned.winfo_width()
        if total_width <= 1:
            self.after(50, lambda: self._place_sash(x))
            return
        x = max(0, min(x, max(0, total_width - 1)))
        try:
            self.main_paned.sash_place(0, x, 0)
        except Exception:
            pass

    def _resolve_paned_bg(self) -> str:
        mode = ctk.get_appearance_mode()
        if mode == "Light":
            return "#e8ebf0"
        return Color.BG_MAIN_DARK

    def refresh_theme(self) -> None:
        self.main_paned.configure(bg=self._resolve_paned_bg())
