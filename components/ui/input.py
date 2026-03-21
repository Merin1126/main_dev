from typing import Callable, Any
import customtkinter as ctk
from config.settings import Color

class Input(ctk.CTkEntry):
    def __init__(
        self,
        master,
        width: int = 140,
        height: int = 35,
        corner_radius: int | None = 10,
        border_width: int | None = 2,
        bg_color: str | tuple = Color.TRANSPARENT,
        defaultValue: str = "",
        on_change_callback: Callable = None,
        **kwargs,
    ) -> None:
        self.on_change_callback = on_change_callback

        # 绑定字符串变量，方便实时获取和监听
        self.var = ctk.StringVar()
        self.var.trace_add("write", self.onChange)

        super().__init__(
            master=master,
            width=width,
            height=height,
            corner_radius=corner_radius,
            border_width=border_width,
            bg_color=bg_color,
            textvariable=self.var,
            **kwargs,
        )
        self.var.set(defaultValue)

    def getValue(self) -> str:
        """快速获取输入框文本"""
        return self.var.get()

    def setValue(self, value: str) -> None:
        """快速设置输入框文本"""
        self.var.set(value)

    def clear(self) -> None:
        """清空输入框"""
        self.var.set("")
        self.update()

    def onChange(self, *args: tuple[Any, ...]) -> None:
        if self.on_change_callback is not None:
            self.on_change_callback(self.var)