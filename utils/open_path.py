"""在系统默认应用中打开文件或文件夹。"""
from __future__ import annotations

import os
import subprocess
import sys


def open_path_in_system(path: str) -> None:
    """用系统默认方式打开文件（或文件夹）。"""
    target = os.path.abspath(path)
    if not os.path.exists(target):
        raise FileNotFoundError(target)
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", target], check=True)
    elif os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", target], check=True)


def reveal_file_in_folder(path: str) -> None:
    """在文件管理器中打开所在文件夹并选中该文件。"""
    target = os.path.abspath(path)
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", "-R", target], check=True)
    elif os.name == "nt":
        subprocess.run(["explorer", "/select,", target], check=True)
    else:
        subprocess.run(["xdg-open", os.path.dirname(target)], check=True)
