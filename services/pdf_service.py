from __future__ import annotations

from typing import Optional

import fitz
from PIL import Image, ImageTk


class PdfService:
    """封装 PDF 打开、渲染与页面字节提取。"""

    def __init__(self) -> None:
        self._doc: Optional[fitz.Document] = None

    def open_pdf(self, path: str) -> fitz.Document:
        self.close()
        self._doc = fitz.open(path)
        return self._doc

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def get_page_count(self) -> int:
        return len(self._doc) if self._doc is not None else 0

    def count_pages(self, path: str) -> int:
        with fitz.open(path) as doc:
            return len(doc)

    def render_page_image(self, page_index: int, zoom_factor: float) -> ImageTk.PhotoImage:
        if self._doc is None:
            raise RuntimeError("PDF 尚未打开。")
        page = self._doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom_factor, zoom_factor))
        mode = "RGBA" if pix.alpha else "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        return ImageTk.PhotoImage(img)

    def get_page_bytes(self, page_index: int) -> bytes:
        if self._doc is None:
            raise RuntimeError("PDF 尚未打开。")
        page = self._doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        return pix.tobytes("png")
