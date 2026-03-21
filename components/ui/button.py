from typing import Callable, Literal
import customtkinter as ctk
from config.settings import Color

class Button(ctk.CTkButton):
    def __init__(
        self,
        master,
        text: str = "Button",
        textColor: str | tuple = Color.TEXT,
        fontFamily: str = "Arial",
        fontSize: int = 14,
        fontWeight: Literal["normal", "bold"] = "bold",
        width: int = 300,
        height: int = 40,
        command: Callable = lambda: print("Button pressed!"),
        **kwargs,
    ) -> None:
        
        # 自动帮你把字体和字号打包好
        font = ctk.CTkFont(family=fontFamily, size=fontSize, weight=fontWeight)

        super().__init__(
            master=master,
            width=width,
            height=height,
            text=text,
            font=font,
            text_color=textColor,
            command=command,
            **kwargs,
        )