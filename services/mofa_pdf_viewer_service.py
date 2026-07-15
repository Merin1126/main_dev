"""PDF page rendering and OCR bbox transforms for the MOFA search viewer."""
from __future__ import annotations

import math
from dataclasses import dataclass

import fitz
from PIL import Image


@dataclass(frozen=True)
class MofaRenderedPage:
    image: Image.Image
    page_width: float
    page_height: float
    render_scale: float
    page_count: int


class MofaPdfViewerService:
    MIN_ZOOM = 0.5
    MAX_ZOOM = 3.0
    DEFAULT_SUPERSAMPLE = 2.0
    MAX_RENDER_PIXELS = 28_000_000

    @classmethod
    def clamp_zoom(cls, value: float) -> float:
        return max(cls.MIN_ZOOM, min(cls.MAX_ZOOM, float(value)))

    @classmethod
    def render_page(
        cls,
        path: str,
        page_index: int,
        viewport_width: int,
        viewport_height: int,
        *,
        zoom: float = 1.0,
        fit_mode: str = "width",
        supersample: float = DEFAULT_SUPERSAMPLE,
    ) -> MofaRenderedPage:
        with fitz.open(path) as document:
            page_count = len(document)
            if page_index < 0 or page_index >= page_count:
                raise IndexError(f"PDF page_index 越界：{page_index}/{page_count}")
            page = document[page_index]
            width = float(page.rect.width)
            height = float(page.rect.height)
            available_width = max(80.0, float(viewport_width) - 24.0)
            available_height = max(80.0, float(viewport_height) - 24.0)
            if fit_mode == "page":
                fit_scale = min(available_width / width, available_height / height)
            elif fit_mode == "width":
                fit_scale = available_width / width
            else:
                raise ValueError(f"未知 PDF 适配模式：{fit_mode}")
            scale = max(0.05, fit_scale * cls.clamp_zoom(zoom))
            target_size = (
                max(1, round(width * scale)),
                max(1, round(height * scale)),
            )
            target_pixels = max(1, target_size[0] * target_size[1])
            safe_quality = math.sqrt(cls.MAX_RENDER_PIXELS / target_pixels)
            quality = max(1.0, min(3.0, float(supersample), safe_quality))
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale * quality, scale * quality),
                alpha=False,
            )
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            if image.size != target_size:
                image = image.resize(target_size, Image.Resampling.LANCZOS)
        return MofaRenderedPage(
            image=image,
            page_width=width,
            page_height=height,
            render_scale=scale,
            page_count=page_count,
        )

    @staticmethod
    def bbox_for_view(
        bbox: tuple[float, float, float, float],
        *,
        source_region: str,
        original_view: bool,
        split_ratio: float = 0.5,
    ) -> tuple[float, float, float, float]:
        """Map a single-page normalized bbox into the selected PDF page space."""
        x0, y0, x1, y1 = bbox
        if not original_view or source_region in {"", "full"}:
            return x0, y0, x1, y1
        ratio = max(0.05, min(0.95, float(split_ratio)))
        if source_region == "right":
            return (
                ratio + x0 * (1.0 - ratio),
                y0,
                ratio + x1 * (1.0 - ratio),
                y1,
            )
        if source_region == "left":
            return x0 * ratio, y0, x1 * ratio, y1
        return x0, y0, x1, y1

    @classmethod
    def bbox_on_canvas(
        cls,
        bbox: tuple[float, float, float, float],
        *,
        image_width: int,
        image_height: int,
        offset_x: float,
        offset_y: float,
        source_region: str = "full",
        original_view: bool = False,
        split_ratio: float = 0.5,
    ) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = cls.bbox_for_view(
            bbox,
            source_region=source_region,
            original_view=original_view,
            split_ratio=split_ratio,
        )
        return (
            offset_x + x0 * image_width,
            offset_y + y0 * image_height,
            offset_x + x1 * image_width,
            offset_y + y1 * image_height,
        )
