"""文本编辑器内 Cmd/Ctrl+F 搜索定位。"""
from __future__ import annotations

import sys
import tkinter as tk

import customtkinter as ctk

from components.ui.button import Button
from config.settings import Color


class EditorSearchController:
    """为 CTkTextbox 绑定快捷键搜索条（⌘F / Ctrl+F）。"""

    def __init__(self, host: ctk.CTkFrame, textbox: ctk.CTkTextbox) -> None:
        self.host = host
        self.textbox = textbox
        self._inner = textbox._textbox
        self._visible = False
        self._last_query = ""
        self._escape_targets: tuple[tk.Misc, ...] = ()

        self.bar = ctk.CTkFrame(host, fg_color=("#e8edf2", "#2a3038"), corner_radius=8)
        row = ctk.CTkFrame(self.bar, fg_color=Color.TRANSPARENT)
        row.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(row, text="查找", width=36, anchor="w").pack(side="left")
        self.query_var = ctk.StringVar()
        self.entry = ctk.CTkEntry(row, textvariable=self.query_var, width=180, placeholder_text="输入关键词")
        self.entry.pack(side="left", padx=(4, 6))
        self.entry.bind("<Return>", lambda _e: self.find_next())
        self.entry.bind("<Shift-Return>", lambda _e: self.find_previous())
        Button(row, text="下一个", width=64, height=28, command=self.find_next).pack(side="left", padx=2)
        Button(row, text="上一个", width=64, height=28, command=self.find_previous).pack(side="left", padx=2)
        Button(row, text="关闭", width=52, height=28, command=self.hide).pack(side="left", padx=(6, 0))

        self._inner.tag_configure("editor_search_match", background="#ffe566", foreground="#1a1a1a")

    def attach(self) -> None:
        find_binds = ("<Control-f>", "<Command-f>")
        for widget in (self.host, self.textbox, self._inner):
            for seq in find_binds:
                widget.bind(seq, self._on_find_shortcut, add="+")
        self.entry.bind("<Escape>", self._on_escape, add="+")

    def _on_find_shortcut(self, event=None) -> str:
        if self._visible:
            self.hide()
        else:
            self.show()
        return "break"

    def _on_escape(self, event=None) -> str:
        if self._visible:
            self.hide()
        return "break"

    def _bind_escape_shortcut(self) -> None:
        self._escape_targets = (self.bar, self.entry, self.host, self.textbox, self._inner)
        for widget in self._escape_targets:
            widget.bind("<Escape>", self._on_escape, add="+")

    def _unbind_escape_shortcut(self) -> None:
        for widget in self._escape_targets:
            try:
                widget.unbind("<Escape>")
            except tk.TclError:
                pass
        self._escape_targets = ()

    def show(self) -> None:
        if not self._visible:
            self.bar.pack(fill="x", padx=10, pady=(0, 4), before=self.textbox)
            self._visible = True
            self._bind_escape_shortcut()
        self.entry.focus_set()
        if self.query_var.get().strip():
            self.find_next()

    def hide(self) -> None:
        if self._visible:
            self.bar.pack_forget()
            self._visible = False
            self._unbind_escape_shortcut()
        self._clear_highlight()
        self.textbox.focus_set()

    def _clear_highlight(self) -> None:
        self._inner.tag_remove("editor_search_match", "1.0", "end")

    def _search(self, *, backward: bool) -> bool:
        query = (self.query_var.get() or "").strip()
        if not query:
            return False
        self._clear_highlight()
        inner = self._inner
        if backward:
            start = inner.index("insert-1c")
            idx = inner.search(query, start, stopindex="1.0", backwards=True, nocase=True)
            if not idx:
                idx = inner.search(query, "end-1c", stopindex="1.0", backwards=True, nocase=True)
        else:
            if self._last_query != query:
                start = "1.0"
            else:
                start = inner.index("insert+1c")
            idx = inner.search(query, start, stopindex="end", nocase=True)
            if not idx:
                idx = inner.search(query, "1.0", stopindex="end", nocase=True)
        self._last_query = query
        if not idx:
            if sys.platform == "darwin":
                inner.bell()
            return False
        end = f"{idx}+{len(query)}c"
        inner.tag_add("editor_search_match", idx, end)
        inner.mark_set("insert", idx)
        inner.see(idx)
        return True

    def find_next(self) -> None:
        if not self._search(backward=False):
            pass

    def find_previous(self) -> None:
        self._search(backward=True)
