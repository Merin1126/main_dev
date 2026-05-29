import customtkinter as ctk
from config.settings import Color, APP_VERSION

class Navigation(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        # 继承原作者的深色导航栏风格
        super().__init__(master, width=84, corner_radius=0, fg_color=Color.BG_NAV_DARK, **kwargs)
        self.master = master
        self.expanded_width = 150
        self.collapsed_width = 84
        self.current_width = self.collapsed_width
        self.is_expanded = False
        self._animating = False
        self._animation_total_steps = 10
        self._animation_duration_ms = 140
        self._last_applied_width = self.collapsed_width
        self.current_screen = "scraper"

        self.nav_config = [
            {"icon": "\uf409", "label": "史料下载", "screen": "scraper"},
            {"icon": "\uf05f", "label": "史料校对", "screen": "ocr"},
            {"icon": "\uf080", "label": "史料分析", "screen": "analysis"},
            {"icon": "\uf1ab", "label": "史料翻译", "screen": "translation"},
            {"icon": "\ue690", "label": "系统设置", "screen": "setting"},
        ]
        self.nav_buttons = {}
        self.pack_propagate(False)

        # ================= 顶部 Logo/标题区 =================
        self.title_container = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        self.title_container.pack(pady=(20, 0), padx=10)

        self.org_line1_label = ctk.CTkLabel(
            self.title_container,
            text="四川大学",
            font=("PingFang SC", 12, "bold"),
            text_color=Color.NAV_ORG_LINE1,
        )
        self.org_line2_label = ctk.CTkLabel(
            self.title_container,
            text="日本研究中心",
            font=("PingFang SC", 10),
            text_color=Color.NAV_ORG_LINE2,
        )
        self.org_divider = ctk.CTkFrame(
            self.title_container,
            height=1,
            width=48,
            fg_color=Color.NAV_ORG_DIVIDER,
            corner_radius=0,
        )

        self.title_label = ctk.CTkLabel(
            self.title_container, text="HRS",
            font=("Arial", 26, "bold"), text_color=Color.TEXT_WHITE, cursor="hand2"
        )
        self.title_label.pack()

        self.version_label = ctk.CTkLabel(
            self.title_container, text=APP_VERSION,
            font=("Arial", 11), text_color=Color.TEXT_MUTED
        )
        self.version_label.pack(pady=(2, 0))

        self.menu_icon_label = ctk.CTkLabel(
            self.title_container,
            text="\U000f035c",
            font=("Symbols Nerd Font", 24),
            text_color=Color.TEXT_WHITE,
            cursor="hand2"
        )
        self.menu_icon_label.pack(pady=(6, 5))
        self.menu_icon_label.bind("<Button-1>", self.toggle_navigation)

        # ================= 中间 导航按钮区 =================
        self.nav_btn_container = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        self.nav_btn_container.pack(fill="x", padx=10, pady=(0, 8))

        for item in self.nav_config:
            btn = ctk.CTkButton(
                self.nav_btn_container,
                text=item["icon"],
                font=("Symbols Nerd Font", 18),
                fg_color=Color.TRANSPARENT,
                text_color=Color.TEXT_MUTED,
                hover_color=Color.BG_HOVER,
                height=44,
                corner_radius=14,
                anchor="center",
                command=lambda screen=item["screen"]: self.on_nav_item_click(screen)
            )
            btn.pack(pady=5, padx=6, fill="x")
            self.nav_buttons[item["screen"]] = btn

        # ================= 底部 主题切换区 =================
        # 保留原作者切换 Light/Dark 的优良传统
        self.appearance_mode_menu = ctk.CTkOptionMenu(
            self, values=["System", "Dark", "Light"],
            command=self.change_appearance_mode_event,
            fg_color=Color.BG_HOVER, button_color=Color.PRIMARY
        )
        self.appearance_mode_menu.pack(side="bottom", pady=30, padx=15, fill="x")
        self.appearance_mode_menu.pack_forget()

        self._render_nav_buttons()
        self._render_title_branding(self.is_expanded)
        self.navigate("scraper")
        self.after(0, self._ensure_initial_collapsed_width)

    def navigate(self, screen_name: str) -> None:
        """
        核心路由与按钮高亮逻辑（致敬原代码的 getObjectNavButtonCurrentScreen）
        """
        self.current_screen = screen_name

        # 1. 重置所有按钮为未激活状态 (暗灰色)
        for btn in self.nav_buttons.values():
            btn.configure(fg_color=Color.TRANSPARENT, text_color=Color.TEXT_MUTED)

        # 2. 高亮当前点击的按钮 (亮蓝色底，白字)
        if screen_name in self.nav_buttons:
            self.nav_buttons[screen_name].configure(fg_color=Color.PRIMARY, text_color=Color.TEXT_WHITE)

        # 3. 通知右侧的“屏幕大管家”切换页面
        if hasattr(self.master, "screen_manager"):
            self.master.screen_manager.change_screen(screen_name)

    def change_appearance_mode_event(self, new_appearance_mode: str):
        """处理主题颜色切换"""
        ctk.set_appearance_mode(new_appearance_mode)
        if hasattr(self.master, "screen_manager") and hasattr(self.master.screen_manager, "refresh_theme"):
            self.master.screen_manager.refresh_theme()

    def _ensure_initial_collapsed_width(self):
        """确保启动时就使用折叠宽度，避免首次布局抖动。"""
        if self.is_expanded or self._animating:
            return
        self.current_width = self.collapsed_width
        self._last_applied_width = self.collapsed_width
        self.configure(width=self.collapsed_width)
        self.pack_propagate(False)

    def on_nav_item_click(self, screen_name: str):
        if self._animating:
            return
        if not self.is_expanded:
            # 折叠态：立即切页，同时启动展开动画
            self.navigate(screen_name)
            self.toggle_navigation()
            return
        self.navigate(screen_name)

    def toggle_navigation(self, _event=None):
        if self._animating:
            return
        target_width = self.expanded_width if not self.is_expanded else self.collapsed_width
        if target_width == self.expanded_width:
            # 展开动画开始即显示文字，并从低亮度淡入
            self._set_buttons_expanded_text(True)
            self._set_button_text_colors(0)
            self.appearance_mode_menu.pack_forget()
        else:
            # 收起时直接降亮度，避免与宽度动画并发造成掉帧
            self._set_button_text_colors(0.0)
        self._animate_width(target_width, 0)

    def _animate_width(self, target_width, step_index):
        self._animating = True
        start_width = self.collapsed_width if target_width > self.current_width else self.expanded_width
        t = min(1.0, step_index / self._animation_total_steps)
        eased_t = 1 - pow(1 - t, 3)  # ease-out cubic
        self.current_width = int(start_width + (target_width - start_width) * eased_t)
        if self.current_width != self._last_applied_width:
            self.configure(width=self.current_width)
            self._last_applied_width = self.current_width
        self.pack_propagate(False)

        if step_index >= self._animation_total_steps:
            self.current_width = target_width
            self.configure(width=self.current_width)
            self._last_applied_width = self.current_width
            self.is_expanded = (target_width == self.expanded_width)
            self._render_nav_buttons()
            self._render_title_branding(self.is_expanded)
            if self.is_expanded:
                self._set_button_text_colors(1.0)
            self._animating = False
            return

        frame_delay = max(8, int(self._animation_duration_ms / self._animation_total_steps))
        self.after(frame_delay, lambda: self._animate_width(target_width, step_index + 1))

    def _animate_label_fade_in(self, fade_step):
        if not self.is_expanded:
            return
        total_fade_steps = 6
        ratio = min(1.0, fade_step / total_fade_steps)
        self._set_button_text_colors(ratio)
        if fade_step < total_fade_steps:
            self.after(20, lambda: self._animate_label_fade_in(fade_step + 1))

    def _animate_label_fade_out(self, fade_step):
        if not self.is_expanded or self._animating:
            return
        total_fade_steps = 3
        ratio = max(0.0, 1.0 - (fade_step / total_fade_steps))
        self._set_button_text_colors(ratio)
        if fade_step < total_fade_steps:
            self.after(12, lambda: self._animate_label_fade_out(fade_step + 1))

    def _set_buttons_expanded_text(self, expanded):
        for item in self.nav_config:
            btn = self.nav_buttons[item["screen"]]
            if expanded:
                btn.configure(
                    text=f'{item["icon"]}  {item["label"]}',
                    font=("Symbols Nerd Font", 15, "bold"),
                    anchor="w"
                )
            else:
                btn.configure(text=item["icon"], font=("Symbols Nerd Font", 18), anchor="center")

    def _blend_hex(self, start_hex, end_hex, ratio):
        def _hex_to_rgb(hex_color):
            hex_color = hex_color.lstrip("#")
            return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

        def _rgb_to_hex(rgb):
            return "#{:02x}{:02x}{:02x}".format(rgb[0], rgb[1], rgb[2])

        s = _hex_to_rgb(start_hex)
        e = _hex_to_rgb(end_hex)
        mixed = tuple(int(s[i] + (e[i] - s[i]) * ratio) for i in range(3))
        return _rgb_to_hex(mixed)

    def _set_button_text_colors(self, ratio):
        inactive = self._blend_hex(Color.NAV_TEXT_INACTIVE_START, Color.NAV_TEXT_INACTIVE_END, ratio)
        active = self._blend_hex(Color.NAV_TEXT_ACTIVE_START, Color.NAV_TEXT_ACTIVE_END, ratio)
        for item in self.nav_config:
            btn = self.nav_buttons[item["screen"]]
            if item["screen"] == self.current_screen:
                btn.configure(text_color=active)
            else:
                btn.configure(text_color=inactive)

    def _render_title_branding(self, expanded: bool) -> None:
        """折叠：仅「四川大学」+ HRS；展开：两行机构名 + 分割线 + HRS。"""
        for widget in (self.org_line1_label, self.org_line2_label, self.org_divider):
            widget.pack_forget()
        if expanded:
            self.org_line1_label.configure(
                text="四川大学",
                font=("PingFang SC", 12, "bold"),
            )
            self.org_line1_label.pack(before=self.title_label)
            self.org_line2_label.pack(after=self.org_line1_label, pady=(2, 0))
            self.org_divider.pack(after=self.org_line2_label, pady=(6, 4))
            return
        # 折叠态（84px）：单行「四川大学」可放入内容区，副标题与分割线隐藏。
        self.org_line1_label.configure(
            text="四川大学",
            font=("PingFang SC", 10, "bold"),
        )
        self.org_line1_label.pack(before=self.title_label, pady=(0, 4))

    def _render_nav_buttons(self):
        expanded = self.is_expanded
        self._set_buttons_expanded_text(expanded)

        if expanded:
            self.appearance_mode_menu.pack(side="bottom", pady=30, padx=15, fill="x")
        else:
            self.appearance_mode_menu.pack_forget()

        self.navigate(self.current_screen)