"""User-facing maintenance window for the versioned MOFA search lexicon."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from config.settings import Color
from services.mofa_search_lexicon_service import (
    CATEGORY_ALIAS,
    CATEGORY_GLYPH,
    CATEGORY_LABELS,
    CATEGORY_OCR,
    CATEGORY_RELATED,
    MofaLexiconRule,
    MofaSearchLexiconService,
)


_CATEGORY_BY_LABEL = {
    "新旧字体": CATEGORY_GLYPH,
    "OCR混淆": CATEGORY_OCR,
    "历史术语": CATEGORY_ALIAS,
    "关联概念": CATEGORY_RELATED,
}
_STATUS_BY_LABEL = {"全部状态": None, "已启用": True, "已停用": False}


class MofaLexiconDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        *,
        service: MofaSearchLexiconService,
        on_changed=None,
        initial_source: str = "",
        initial_category: str = "",
    ) -> None:
        super().__init__(master)
        self.service = service
        self.on_changed = on_changed
        self.rules: list[MofaLexiconRule] = []
        self.current_rule_id = ""
        self.title("MOFA 检索词库管理")
        self.geometry("1180x720")
        self.minsize(920, 600)
        self.transient(master.winfo_toplevel())
        self._build_ui()
        self.refresh()
        if initial_source:
            self.new_rule()
            self.source_var.set(initial_source.strip())
            if initial_category in CATEGORY_LABELS:
                self.category_var.set(CATEGORY_LABELS[initial_category])

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=18, pady=(16, 8))
        title_box = ctk.CTkFrame(header, fg_color=Color.TRANSPARENT)
        title_box.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_box,
            text="MOFA 检索词库",
            font=("PingFang SC", 20, "bold"),
            anchor="w",
        ).pack(fill="x")
        self.summary_label = ctk.CTkLabel(
            title_box,
            text="",
            text_color=Color.TEXT_GRAY,
            anchor="w",
        )
        self.summary_label.pack(fill="x", pady=(2, 0))
        ctk.CTkButton(
            header, text="导出", width=72, command=self.export_rules
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            header, text="版本历史", width=84, command=self.show_history
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            header, text="导入", width=72, command=self.import_rules
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            header, text="新增规则", width=88, command=self.new_rule
        ).pack(side="right", padx=(6, 0))

        filters = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        filters.pack(fill="x", padx=18, pady=(0, 8))
        self.filter_text_var = tk.StringVar()
        entry = ctk.CTkEntry(
            filters,
            textvariable=self.filter_text_var,
            placeholder_text="搜索词语、说明或出处",
            height=34,
        )
        entry.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        entry.bind("<Return>", lambda _event: self.refresh())
        self.filter_category_var = tk.StringVar(value="全部类别")
        ctk.CTkOptionMenu(
            filters,
            values=["全部类别", *_CATEGORY_BY_LABEL],
            variable=self.filter_category_var,
            width=110,
            command=lambda _value: self.refresh(),
        ).pack(side="left", padx=6, pady=8)
        self.filter_status_var = tk.StringVar(value="全部状态")
        ctk.CTkOptionMenu(
            filters,
            values=list(_STATUS_BY_LABEL),
            variable=self.filter_status_var,
            width=100,
            command=lambda _value: self.refresh(),
        ).pack(side="left", padx=6, pady=8)
        ctk.CTkButton(
            filters, text="筛选", width=68, command=self.refresh
        ).pack(side="left", padx=(6, 10), pady=8)

        body = tk.PanedWindow(
            self,
            orient="horizontal",
            sashwidth=7,
            sashrelief="flat",
            borderwidth=0,
            bg=Color.BG_MAIN_DARK,
        )
        body.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        table_frame = ctk.CTkFrame(body, fg_color=Color.BG_CARD, corner_radius=10)
        editor = ctk.CTkFrame(body, fg_color=Color.BG_CARD, corner_radius=10)
        body.add(table_frame, minsize=540, stretch="always")
        body.add(editor, minsize=330, width=390, stretch="never")

        columns = ("category", "source", "direction", "target", "weight", "status", "origin")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        labels = {
            "category": "类别",
            "source": "来源词",
            "direction": "方向",
            "target": "目标词",
            "weight": "权重",
            "status": "状态",
            "origin": "来源",
        }
        widths = {
            "category": 90,
            "source": 140,
            "direction": 48,
            "target": 140,
            "weight": 55,
            "status": 65,
            "origin": 70,
        }
        for key in columns:
            self.tree.heading(key, text=labels[key])
            self.tree.column(key, width=widths[key], minwidth=45, stretch=key in {"source", "target"})
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
        self.tree.bind("<<TreeviewSelect>>", self._select_rule)

        ctk.CTkLabel(
            editor,
            text="规则编辑",
            font=("PingFang SC", 15, "bold"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 8))
        self.rule_state_label = ctk.CTkLabel(
            editor,
            text="新增自定义规则",
            anchor="w",
            text_color=Color.TEXT_GRAY,
        )
        self.rule_state_label.pack(fill="x", padx=14, pady=(0, 8))

        self.category_var = tk.StringVar(value="新旧字体")
        self.category_menu = self._field_menu(editor, "类别", self.category_var, list(_CATEGORY_BY_LABEL))
        self.source_var = tk.StringVar()
        self.source_entry = self._field_entry(editor, "来源词", self.source_var)
        self.target_var = tk.StringVar()
        self.target_entry = self._field_entry(editor, "目标词", self.target_var)
        self.bidirectional_var = tk.BooleanVar(value=True)
        self.bidirectional_check = ctk.CTkCheckBox(
            editor,
            text="双向扩展（来源词 ↔ 目标词）",
            variable=self.bidirectional_var,
        )
        self.bidirectional_check.pack(fill="x", padx=14, pady=(4, 8))
        self.weight_var = tk.StringVar(value="1.0")
        self.weight_entry = self._field_entry(editor, "权重（0—1）", self.weight_var)
        self.notes_var = tk.StringVar()
        self.notes_entry = self._field_entry(editor, "研究说明", self.notes_var)
        self.provenance_var = tk.StringVar()
        self.provenance_entry = self._field_entry(editor, "出处／依据", self.provenance_var)

        ctk.CTkLabel(
            editor,
            text="每次保存、启停或删除都会形成新的完整词库版本。内置规则属于 OCR Generation 的既有规范化基础，只读。",
            wraplength=350,
            justify="left",
            anchor="w",
            text_color=Color.TEXT_GRAY,
        ).pack(fill="x", padx=14, pady=(8, 10))

        actions = ctk.CTkFrame(editor, fg_color=Color.TRANSPARENT)
        actions.pack(side="bottom", fill="x", padx=14, pady=14)
        self.save_btn = ctk.CTkButton(actions, text="保存规则", command=self.save_rule)
        self.save_btn.pack(fill="x", pady=(0, 6))
        row = ctk.CTkFrame(actions, fg_color=Color.TRANSPARENT)
        row.pack(fill="x")
        self.toggle_btn = ctk.CTkButton(
            row, text="停用", width=100, command=self.toggle_rule, state="disabled"
        )
        self.toggle_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.delete_btn = ctk.CTkButton(
            row,
            text="删除",
            width=100,
            fg_color="#b84040",
            hover_color="#913232",
            command=self.delete_rule,
            state="disabled",
        )
        self.delete_btn.pack(side="left", fill="x", expand=True, padx=(3, 0))

    @staticmethod
    def _field_entry(master, label: str, variable: tk.StringVar):
        ctk.CTkLabel(master, text=label, anchor="w").pack(fill="x", padx=14, pady=(6, 2))
        widget = ctk.CTkEntry(master, textvariable=variable, height=32)
        widget.pack(fill="x", padx=14)
        return widget

    @staticmethod
    def _field_menu(master, label: str, variable: tk.StringVar, values: list[str]):
        ctk.CTkLabel(master, text=label, anchor="w").pack(fill="x", padx=14, pady=(6, 2))
        widget = ctk.CTkOptionMenu(master, values=values, variable=variable, height=32)
        widget.pack(fill="x", padx=14)
        return widget

    def refresh(self, *, keep_rule_id: str = "") -> None:
        category_label = self.filter_category_var.get()
        category = _CATEGORY_BY_LABEL.get(category_label, "")
        status = _STATUS_BY_LABEL[self.filter_status_var.get()]
        self.rules = self.service.list_rules(
            category=category,
            active=status,
            search_text=self.filter_text_var.get(),
        )
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, rule in enumerate(self.rules):
            self.tree.insert(
                "",
                "end",
                iid=f"rule-{index}",
                values=(
                    CATEGORY_LABELS[rule.category],
                    rule.source_term,
                    "↔" if rule.bidirectional else "→",
                    rule.target_term,
                    f"{rule.weight:.2f}",
                    "启用" if rule.active else "停用",
                    "内置" if rule.built_in else "自定义",
                ),
            )
        custom = sum(not rule.built_in for rule in self.service.list_rules())
        active = sum(rule.active for rule in self.service.list_rules())
        self.summary_label.configure(
            text=(
                f"当前版本 r{self.service.current_revision()} · "
                f"{active} 条启用 · {custom} 条自定义 · 当前显示 {len(self.rules)} 条"
            )
        )
        target = keep_rule_id or self.current_rule_id
        for index, rule in enumerate(self.rules):
            if rule.rule_id == target:
                iid = f"rule-{index}"
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
                self._select_rule()
                break
        if callable(self.on_changed):
            self.on_changed()

    def _selected_rule(self) -> MofaLexiconRule | None:
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            return self.rules[int(selection[0].split("-", 1)[1])]
        except (IndexError, ValueError):
            return None

    def _set_editor_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.category_menu,
            self.source_entry,
            self.target_entry,
            self.bidirectional_check,
            self.weight_entry,
            self.notes_entry,
            self.provenance_entry,
            self.save_btn,
        ):
            widget.configure(state=state)

    def _select_rule(self, _event=None) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        self.current_rule_id = rule.rule_id
        self.category_var.set(CATEGORY_LABELS[rule.category])
        self.source_var.set(rule.source_term)
        self.target_var.set(rule.target_term)
        self.bidirectional_var.set(rule.bidirectional)
        self.weight_var.set(f"{rule.weight:g}")
        self.notes_var.set(rule.notes)
        self.provenance_var.set(rule.provenance)
        if rule.built_in:
            self.rule_state_label.configure(text="内置规范化规则（只读）")
            self._set_editor_state(False)
            self.toggle_btn.configure(state="disabled", text="不可停用")
            self.delete_btn.configure(state="disabled")
        else:
            self.rule_state_label.configure(
                text=f"自定义规则 · {'已启用' if rule.active else '已停用'}"
            )
            self._set_editor_state(True)
            self.toggle_btn.configure(
                state="normal", text="停用" if rule.active else "启用"
            )
            self.delete_btn.configure(state="normal")

    def new_rule(self) -> None:
        self.current_rule_id = ""
        self.tree.selection_remove(self.tree.selection())
        self.category_var.set("新旧字体")
        self.source_var.set("")
        self.target_var.set("")
        self.bidirectional_var.set(True)
        self.weight_var.set("1.0")
        self.notes_var.set("")
        self.provenance_var.set("")
        self.rule_state_label.configure(text="新增自定义规则")
        self._set_editor_state(True)
        self.toggle_btn.configure(state="disabled", text="停用")
        self.delete_btn.configure(state="disabled")
        self.source_entry.focus_set()

    def save_rule(self) -> None:
        try:
            values = dict(
                category=_CATEGORY_BY_LABEL[self.category_var.get()],
                source_term=self.source_var.get(),
                target_term=self.target_var.get(),
                bidirectional=self.bidirectional_var.get(),
                weight=float(self.weight_var.get()),
                notes=self.notes_var.get(),
                provenance=self.provenance_var.get(),
            )
            if self.current_rule_id:
                rule = self.service.update_rule(self.current_rule_id, **values)
            else:
                rule = self.service.add_rule(**values)
                self.current_rule_id = rule.rule_id
            self.refresh(keep_rule_id=rule.rule_id)
        except (KeyError, TypeError, ValueError) as exc:
            messagebox.showerror("无法保存规则", str(exc), parent=self)

    def toggle_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None or rule.built_in:
            return
        try:
            updated = self.service.set_active(rule.rule_id, not rule.active)
            self.refresh(keep_rule_id=updated.rule_id)
        except ValueError as exc:
            messagebox.showerror("无法更新规则", str(exc), parent=self)

    def delete_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None or rule.built_in:
            return
        if not messagebox.askyesno(
            "确认删除",
            f"删除规则“{rule.source_term} {'↔' if rule.bidirectional else '→'} {rule.target_term}”？\n"
            "旧词库版本仍会保留该规则快照。",
            parent=self,
        ):
            return
        try:
            self.service.delete_rule(rule.rule_id)
            self.new_rule()
            self.refresh()
        except ValueError as exc:
            messagebox.showerror("无法删除规则", str(exc), parent=self)

    def export_rules(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出 MOFA 自定义检索词库",
            defaultextension=".json",
            filetypes=(("JSON 词库", "*.json"), ("CSV 词库", "*.csv")),
        )
        if not path:
            return
        try:
            self.service.export_file(path)
            messagebox.showinfo("导出完成", os.path.basename(path), parent=self)
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)

    def import_rules(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="导入 MOFA 检索词库",
            filetypes=(("词库文件", "*.json *.csv"), ("JSON", "*.json"), ("CSV", "*.csv")),
        )
        if not path:
            return
        try:
            created, skipped = self.service.import_file(path)
            self.refresh()
            messagebox.showinfo(
                "导入完成",
                f"新增 {created} 条 · 跳过重复或无效 {skipped} 条",
                parent=self,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)

    def show_history(self) -> None:
        history = self.service.revision_history()
        window = ctk.CTkToplevel(self)
        window.title("MOFA 检索词库版本历史")
        window.geometry("760x480")
        window.minsize(620, 360)
        window.transient(self)
        current = self.service.current_revision()
        ctk.CTkLabel(
            window,
            text=f"当前版本 r{current} · 恢复旧版本不会删除其后的历史快照",
            anchor="w",
            font=("PingFang SC", 13, "bold"),
        ).pack(fill="x", padx=16, pady=(16, 8))
        frame = ctk.CTkFrame(window, fg_color=Color.BG_CARD, corner_radius=10)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        columns = ("revision", "created", "count", "description")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        labels = {
            "revision": "版本",
            "created": "创建时间",
            "count": "规则数",
            "description": "说明",
        }
        widths = {"revision": 70, "created": 190, "count": 70, "description": 360}
        for key in columns:
            tree.heading(key, text=labels[key])
            tree.column(key, width=widths[key], stretch=key == "description")
        for index, item in enumerate(history):
            tree.insert(
                "",
                "end",
                iid=f"revision-{index}",
                values=(
                    f"r{item['revision']}" + ("（当前）" if int(item["revision"]) == current else ""),
                    item["created_at"],
                    item["rule_count"],
                    item["description"],
                ),
            )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)

        def restore_selected() -> None:
            selection = tree.selection()
            if not selection:
                return
            try:
                item = history[int(selection[0].split("-", 1)[1])]
                revision = int(item["revision"])
            except (IndexError, TypeError, ValueError):
                return
            if revision == self.service.current_revision():
                return
            if not messagebox.askyesno(
                "确认恢复词库",
                f"将当前自定义规则恢复为 r{revision} 的状态？\n"
                "内置规则保持不变，后续历史快照不会被删除。",
                parent=window,
            ):
                return
            try:
                self.service.restore_revision(revision)
                window.destroy()
                self.new_rule()
                self.refresh()
            except ValueError as exc:
                messagebox.showerror("恢复失败", str(exc), parent=window)

        ctk.CTkButton(
            window,
            text="恢复所选版本",
            width=120,
            command=restore_selected,
        ).pack(side="right", padx=16, pady=(0, 14))
