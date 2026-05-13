from __future__ import annotations

from typing import Callable, Optional


class AppState:
    """全局应用状态（Singleton），负责跨页面共享当前选中文件。"""

    _instance: Optional["AppState"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.selected_pdf_path = None
            cls._instance._file_change_subscribers = []
        return cls._instance

    def subscribe_file_change(self, callback: Callable[[str], None]) -> None:
        if callback not in self._file_change_subscribers:
            self._file_change_subscribers.append(callback)

    def set_selected_pdf(self, path: str) -> None:
        if not path:
            return
        if self.selected_pdf_path == path:
            return
        self.selected_pdf_path = path
        for cb in list(self._file_change_subscribers):
            try:
                cb(path)
            except Exception:
                # 不让单个订阅者异常影响其他订阅者
                continue
