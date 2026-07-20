"""Phase 7A dialogs for creating and managing MOFA research work packages."""
from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

import customtkinter as ctk

from config.settings import Color
from services.mofa_candidate_service import MofaCandidate
from services.mofa_research_package_service import (
    MOFA_PACKAGE_SCOPE_CONTEXT,
    MOFA_PACKAGE_SCOPE_FULL_DOCUMENT,
    MofaResearchPackage,
    MofaResearchPackageCreateResult,
    MofaResearchPackagePlan,
    MofaResearchPackageService,
)
from utils.open_path import open_path_in_system, reveal_file_in_folder


_PACKAGE_STATUS_LABELS = {
    "draft": "草稿",
    "ready": "待处理",
    "processing": "处理中",
    "completed": "已完成",
    "archived": "已归档",
}
_PACKAGE_STATUS_FILTERS = {"全部状态": "", **{value: key for key, value in _PACKAGE_STATUS_LABELS.items()}}


class MofaResearchPackageCreateDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        *,
        service: MofaResearchPackageService,
        candidates: list[MofaCandidate],
        on_created=None,
    ) -> None:
        super().__init__(master)
        self.service = service
        self.candidates = candidates
        self.candidate_ids = tuple(item.candidate_id for item in candidates)
        self.on_created = on_created
        self.plan: MofaResearchPackagePlan | None = None
        self.result: MofaResearchPackageCreateResult | None = None
        self._last_default_name = ""
        self.title("创建 MOFA 研究工作包")
        self.geometry("980x680")
        self.minsize(800, 560)
        self.transient(master.winfo_toplevel())
        self._build_ui()
        self._refresh_preview(initial=True)

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            header,
            text="创建 MOFA 研究工作包",
            font=("PingFang SC", 20, "bold"),
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            header,
            text=(
                f"已选择 {len(self.candidates)} 个 MOFA 候选页。"
                "工作包只保存来源页段引用，不复制史料 PDF。"
            ),
            text_color=Color.TEXT_GRAY,
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        settings = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        settings.pack(fill="x", padx=18, pady=(0, 8))
        top = ctk.CTkFrame(settings, fg_color=Color.TRANSPARENT)
        top.pack(fill="x", padx=12, pady=(10, 6))
        ctk.CTkLabel(top, text="工作包名称", width=84, anchor="w").pack(side="left")
        self.name_var = tk.StringVar()
        self.name_entry = ctk.CTkEntry(top, textvariable=self.name_var)
        self.name_entry.pack(side="left", fill="x", expand=True, padx=(6, 12))
        ctk.CTkLabel(top, text="前置页", width=52).pack(side="left")
        self.before_var = tk.StringVar(value="1")
        self.before_entry = ctk.CTkEntry(top, textvariable=self.before_var, width=54)
        self.before_entry.pack(side="left", padx=(4, 10))
        ctk.CTkLabel(top, text="后置页", width=52).pack(side="left")
        self.after_var = tk.StringVar(value="1")
        self.after_entry = ctk.CTkEntry(top, textvariable=self.after_var, width=54)
        self.after_entry.pack(side="left", padx=(4, 10))
        ctk.CTkButton(
            top,
            text="更新页段预览",
            width=110,
            command=self._refresh_preview,
        ).pack(side="left")

        scope_row = ctk.CTkFrame(settings, fg_color=Color.TRANSPARENT)
        scope_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(scope_row, text="纳入范围", width=84, anchor="w").pack(side="left")
        self.scope_var = tk.StringVar(value="候选页段")
        self.scope_control = ctk.CTkSegmentedButton(
            scope_row,
            values=["候选页段", "整份PDF"],
            variable=self.scope_var,
            command=self._scope_changed,
            width=220,
        )
        self.scope_control.pack(side="left", padx=(6, 12))
        self.scope_help = ctk.CTkLabel(
            scope_row,
            text="候选页段会按前后文扩展；整份PDF会纳入所选候选涉及文书的全部页。",
            text_color=Color.TEXT_GRAY,
            anchor="w",
        )
        self.scope_help.pack(side="left", fill="x", expand=True)

        notes_row = ctk.CTkFrame(settings, fg_color=Color.TRANSPARENT)
        notes_row.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkLabel(notes_row, text="工作包说明", width=84, anchor="nw").pack(side="left")
        self.notes_text = ctk.CTkTextbox(notes_row, height=66, wrap="word")
        self.notes_text.pack(side="left", fill="x", expand=True, padx=(6, 0))

        self.preview_summary = ctk.CTkLabel(
            self,
            text="正在计算 MOFA 页段…",
            anchor="w",
            text_color=Color.TEXT_GRAY,
        )
        self.preview_summary.pack(fill="x", padx=20, pady=(2, 6))

        table_frame = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        table_frame.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        columns = ("year", "volume", "document", "selected", "range", "included", "generation")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        labels = {
            "year": "年份",
            "volume": "卷册",
            "document": "MOFA史料",
            "selected": "候选页",
            "range": "纳入页段",
            "included": "页数",
            "generation": "OCR Generation",
        }
        widths = {
            "year": 60,
            "volume": 60,
            "document": 300,
            "selected": 110,
            "range": 100,
            "included": 60,
            "generation": 190,
        }
        for key in columns:
            self.tree.heading(key, text=labels[key])
            self.tree.column(key, width=widths[key], stretch=key in {"document", "generation"})
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)

        footer = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        footer.pack(fill="x", padx=18, pady=(0, 16))
        self.create_btn = ctk.CTkButton(
            footer,
            text="创建 MOFA 研究工作包",
            width=180,
            command=self._create,
        )
        self.create_btn.pack(side="right")
        ctk.CTkButton(
            footer,
            text="取消",
            width=80,
            fg_color=Color.TEXT_GRAY,
            command=self.destroy,
        ).pack(side="right", padx=(0, 8))

    def _context_values(self) -> tuple[int, int]:
        if self._selection_scope() == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT:
            return 0, 0
        try:
            return int(self.before_var.get()), int(self.after_var.get())
        except ValueError as exc:
            raise ValueError("前置页和后置页必须填写整数") from exc

    def _selection_scope(self) -> str:
        return (
            MOFA_PACKAGE_SCOPE_FULL_DOCUMENT
            if self.scope_var.get() == "整份PDF"
            else MOFA_PACKAGE_SCOPE_CONTEXT
        )

    def _scope_changed(self, _value: str) -> None:
        full_document = self._selection_scope() == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT
        state = "disabled" if full_document else "normal"
        self.before_entry.configure(state=state)
        self.after_entry.configure(state=state)
        self._refresh_preview()

    def _refresh_preview(self, _event=None, *, initial: bool = False) -> None:
        try:
            before, after = self._context_values()
            plan = self.service.preview(
                self.candidate_ids,
                context_before=before,
                context_after=after,
                selection_scope=self._selection_scope(),
            )
        except ValueError as exc:
            self.plan = None
            self.create_btn.configure(state="disabled")
            if not initial:
                messagebox.showerror("无法生成页段预览", str(exc), parent=self)
            else:
                self.preview_summary.configure(text=str(exc))
            return
        self.plan = plan
        self.create_btn.configure(state="normal")
        current_name = self.name_var.get().strip()
        if initial or not current_name or current_name == self._last_default_name:
            self.name_var.set(plan.default_display_name)
        self._last_default_name = plan.default_display_name
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for index, item in enumerate(plan.ranges):
            self.tree.insert(
                "",
                "end",
                iid=f"range-{index}",
                values=(
                    item.year,
                    item.volume_code,
                    item.title,
                    "、".join(str(page + 1) for page in item.selected_page_indexes),
                    item.display_range,
                    item.included_page_count,
                    item.generation_id,
                ),
            )
        scope_label = (
            f"整份PDF模式 · {plan.document_count} 份文书全部纳入"
            if plan.selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT
            else f"候选页段模式 · 前 {plan.context_before} / 后 {plan.context_after}"
        )
        self.preview_summary.configure(
            text=(
                f"{plan.package_id} · {plan.document_count} 份 MOFA 史料 · "
                f"{plan.selected_page_count} 个候选页 → {plan.range_count} 个页段 · "
                f"共纳入 {plan.included_page_count} 页 · {scope_label}"
            )
        )

    def _create(self) -> None:
        try:
            before, after = self._context_values()
            result = self.service.create_package(
                self.candidate_ids,
                context_before=before,
                context_after=after,
                selection_scope=self._selection_scope(),
                display_name=self.name_var.get(),
                notes=self.notes_text.get("1.0", "end-1c"),
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("创建 MOFA 工作包失败", str(exc), parent=self)
            return
        self.result = result
        if callable(self.on_created):
            self.on_created(result)
        action = "已创建" if result.created else "已存在，已刷新清单"
        if messagebox.askyesno(
            "MOFA 研究工作包",
            f"{action}：\n{result.package.display_name}\n\n"
            f"目录：{result.package.package_dir}\n\n是否打开工作包目录？",
            parent=self,
        ):
            try:
                open_path_in_system(result.package.package_dir)
            except Exception as exc:
                messagebox.showerror("无法打开工作包目录", str(exc), parent=self)
        self.destroy()


class MofaResearchPackageManagerDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        *,
        service: MofaResearchPackageService,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.service = service
        self.on_changed = on_changed
        self.packages: list[MofaResearchPackage] = []
        self.title("MOFA 研究工作包")
        self.geometry("1100x650")
        self.minsize(840, 520)
        self.transient(master.winfo_toplevel())
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            header,
            text="MOFA 研究工作包",
            font=("PingFang SC", 20, "bold"),
        ).pack(side="left")
        self.status_var = tk.StringVar(value="全部状态")
        ctk.CTkOptionMenu(
            header,
            values=list(_PACKAGE_STATUS_FILTERS),
            variable=self.status_var,
            width=104,
            command=lambda _value: self.refresh(),
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(header, text="刷新", width=72, command=self.refresh).pack(side="right")

        self.summary_label = ctk.CTkLabel(
            self, text="", anchor="w", text_color=Color.TEXT_GRAY
        )
        self.summary_label.pack(fill="x", padx=20, pady=(0, 8))

        pane = tk.PanedWindow(
            self,
            orient="vertical",
            sashwidth=7,
            sashrelief="flat",
            borderwidth=0,
            bg=Color.BG_MAIN_DARK,
        )
        pane.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        table_frame = ctk.CTkFrame(pane, fg_color=Color.BG_CARD, corner_radius=10)
        detail_frame = ctk.CTkFrame(pane, fg_color=Color.BG_CARD, corner_radius=10)
        pane.add(table_frame, minsize=230, stretch="always")
        pane.add(detail_frame, minsize=180, stretch="always")

        columns = ("id", "status", "name", "documents", "ranges", "pages", "created")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        labels = {
            "id": "MOFA工作包ID",
            "status": "状态",
            "name": "名称",
            "documents": "史料",
            "ranges": "页段",
            "pages": "候选/纳入页",
            "created": "创建时间",
        }
        widths = {
            "id": 220,
            "status": 70,
            "name": 330,
            "documents": 60,
            "ranges": 60,
            "pages": 90,
            "created": 180,
        }
        for key in columns:
            self.tree.heading(key, text=labels[key])
            self.tree.column(key, width=widths[key], stretch=key == "name")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
        self.tree.bind("<<TreeviewSelect>>", self._show_selected)
        self.tree.bind("<Button-2>", self._show_context_menu)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Control-Button-1>", self._show_context_menu)
        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(
            label="删除工作包（保留候选）",
            command=lambda: self._delete_selected(cancel_candidates=False),
        )
        self.context_menu.add_command(
            label="删除工作包并取消候选",
            command=lambda: self._delete_selected(cancel_candidates=True),
        )

        self.detail_text = ctk.CTkTextbox(detail_frame, wrap="word")
        self.detail_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.detail_text.configure(state="disabled")

        footer = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        footer.pack(fill="x", padx=18, pady=(0, 16))
        self.open_dir_btn = ctk.CTkButton(
            footer, text="打开 MOFA 工作包目录", width=160, state="disabled", command=self.open_directory
        )
        self.open_dir_btn.pack(side="right")
        self.reveal_manifest_btn = ctk.CTkButton(
            footer, text="定位清单", width=90, state="disabled", command=self.reveal_manifest
        )
        self.reveal_manifest_btn.pack(side="right", padx=(0, 8))
        self.open_manifest_btn = ctk.CTkButton(
            footer, text="打开清单", width=90, state="disabled", command=self.open_manifest
        )
        self.open_manifest_btn.pack(side="right", padx=(0, 8))
        self.ready_btn = ctk.CTkButton(
            footer, text="标为待处理", width=100, state="disabled", command=lambda: self._set_status("ready")
        )
        self.ready_btn.pack(side="left")
        self.draft_btn = ctk.CTkButton(
            footer, text="恢复草稿", width=90, state="disabled", command=lambda: self._set_status("draft")
        )
        self.draft_btn.pack(side="left", padx=(8, 0))
        self.delete_btn = ctk.CTkButton(
            footer,
            text="删除工作包",
            width=110,
            state="disabled",
            fg_color=Color.TEXT_GRAY,
            command=lambda: self._delete_selected(cancel_candidates=False),
        )
        self.delete_btn.pack(side="left", padx=(16, 0))
        self.delete_with_candidates_btn = ctk.CTkButton(
            footer,
            text="删除并取消候选",
            width=140,
            state="disabled",
            fg_color=Color.RED,
            command=lambda: self._delete_selected(cancel_candidates=True),
        )
        self.delete_with_candidates_btn.pack(side="left", padx=(8, 0))

    def refresh(self) -> None:
        status = _PACKAGE_STATUS_FILTERS[self.status_var.get()]
        self.packages = self.service.list_packages(status=status)
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for package in self.packages:
            self.tree.insert(
                "",
                "end",
                iid=package.package_id,
                values=(
                    package.package_id,
                    _PACKAGE_STATUS_LABELS.get(package.status, package.status),
                    package.display_name,
                    package.document_count,
                    package.range_count,
                    f"{package.selected_page_count}/{package.included_page_count}",
                    package.created_at,
                ),
            )
        summary = self.service.summary()
        self.summary_label.configure(
            text=(
                f"当前显示 {len(self.packages)} · 全部 {summary['total']} · "
                f"草稿 {summary['draft']} · 待处理 {summary['ready']} · "
                f"处理中 {summary['processing']} · 已完成 {summary['completed']}"
            )
        )
        self._clear_detail()

    def _selected(self) -> MofaResearchPackage | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return next((item for item in self.packages if item.package_id == selection[0]), None)

    def _show_selected(self, _event=None) -> None:
        package = self._selected()
        if package is None:
            self._clear_detail()
            return
        ranges = self.service.list_ranges(package.package_id)
        lines = [
            package.display_name,
            f"ID：{package.package_id}",
            f"来源：{package.source} / {package.package_type}",
            f"纳入范围：{'整份PDF' if package.selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT else '候选页段'}",
            f"状态：{_PACKAGE_STATUS_LABELS.get(package.status, package.status)}",
            f"目录：{package.package_dir}",
            f"上下文：前 {package.context_before} 页 / 后 {package.context_after} 页",
            f"统计：{package.document_count} 份史料，{package.range_count} 个页段，"
            f"{package.selected_page_count} 个候选页，纳入 {package.included_page_count} 页",
            "",
            "页段：",
        ]
        for item in ranges:
            selected = "、".join(
                str(int(value) + 1)
                for value in json.loads(item["selected_pages_json"] or "[]")
            )
            lines.append(
                f"- {item['gregorian_year']} / {item['volume_code']} / {item['title']} · "
                f"PDF {int(item['start_page_index']) + 1}—{int(item['end_page_index']) + 1} · "
                f"候选页 {selected}"
            )
        if package.notes:
            lines.extend(("", f"说明：{package.notes}"))
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")
        ready = os.path.isdir(package.package_dir)
        manifest_ready = os.path.isfile(package.manifest_path)
        self.open_dir_btn.configure(state="normal" if ready else "disabled")
        self.open_manifest_btn.configure(state="normal" if manifest_ready else "disabled")
        self.reveal_manifest_btn.configure(state="normal" if manifest_ready else "disabled")
        self.ready_btn.configure(state="normal")
        self.draft_btn.configure(state="normal")
        deletable = package.status in {"draft", "ready"}
        self.delete_btn.configure(state="normal" if deletable else "disabled")
        self.delete_with_candidates_btn.configure(state="normal" if deletable else "disabled")

    def _clear_detail(self) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.configure(state="disabled")
        for button in (
            self.open_dir_btn,
            self.open_manifest_btn,
            self.reveal_manifest_btn,
            self.ready_btn,
            self.draft_btn,
            self.delete_btn,
            self.delete_with_candidates_btn,
        ):
            button.configure(state="disabled")

    def _show_context_menu(self, event) -> str:
        package_id = self.tree.identify_row(event.y)
        if not package_id:
            return "break"
        self.tree.selection_set(package_id)
        self.tree.focus(package_id)
        self._show_selected()
        package = self._selected()
        state = "normal" if package and package.status in {"draft", "ready"} else "disabled"
        self.context_menu.entryconfigure(0, state=state)
        self.context_menu.entryconfigure(1, state=state)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def _delete_selected(self, *, cancel_candidates: bool) -> None:
        package = self._selected()
        if package is None:
            return
        if cancel_candidates:
            impact = (
                "将删除工作包记录、manifest 与工作包目录，并撤回仅被该工作包引用的候选页。\n"
                "仍被其他工作包引用的候选页会自动保留。"
            )
        else:
            impact = "将删除工作包记录、manifest 与工作包目录，所有候选页继续保留。"
        if not messagebox.askyesno(
            "确认删除 MOFA 研究工作包",
            f"{package.display_name}\n{package.package_id}\n\n{impact}\n\n"
            "原 PDF、OCR、全文索引和已保存检索不受影响。此操作不可撤销，是否继续？",
            parent=self,
        ):
            return
        try:
            result = self.service.delete_package(
                package.package_id,
                cancel_candidates=cancel_candidates,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法删除 MOFA 工作包", str(exc), parent=self)
            return
        self.refresh()
        if self.on_changed:
            self.on_changed()
        lines = [f"已删除工作包：{result.package_id}"]
        if cancel_candidates:
            lines.append(f"已取消候选：{result.cancelled_candidate_count} 条")
            if result.retained_candidate_count:
                lines.append(f"保留候选：{result.retained_candidate_count} 条")
            if result.retaining_package_ids:
                lines.append(f"仍在引用：{'、'.join(result.retaining_package_ids)}")
        else:
            lines.append(f"保留候选：{result.retained_candidate_count} 条")
        if result.directory_error:
            lines.extend(("", f"本地目录未完全删除：{result.directory_error}"))
            messagebox.showwarning("工作包记录已删除", "\n".join(lines), parent=self)
        else:
            messagebox.showinfo("已删除 MOFA 工作包", "\n".join(lines), parent=self)

    def _set_status(self, status: str) -> None:
        package = self._selected()
        if package is None:
            return
        try:
            self.service.update_status(package.package_id, status)
            self.refresh()
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法更新 MOFA 工作包", str(exc), parent=self)

    def open_directory(self) -> None:
        package = self._selected()
        if package:
            try:
                open_path_in_system(package.package_dir)
            except Exception as exc:
                messagebox.showerror("无法打开工作包目录", str(exc), parent=self)

    def open_manifest(self) -> None:
        package = self._selected()
        if package:
            try:
                open_path_in_system(package.manifest_path)
            except Exception as exc:
                messagebox.showerror("无法打开工作包清单", str(exc), parent=self)

    def reveal_manifest(self) -> None:
        package = self._selected()
        if package:
            try:
                reveal_file_in_folder(package.manifest_path)
            except Exception as exc:
                messagebox.showerror("无法定位工作包清单", str(exc), parent=self)
