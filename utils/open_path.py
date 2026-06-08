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
