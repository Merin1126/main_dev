from __future__ import annotations

import os
import tempfile

from services.mofa_pdf_viewer_service import MofaPdfViewerService
from test_mofa_mineru_normalization import _make_pdf


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "viewer.pdf")
        _make_pdf(path, "VIEW", 2)
        service = MofaPdfViewerService()
        rendered = service.render_page(path, 1, 800, 600, zoom=1.0)
        assert rendered.page_count == 2
        assert rendered.image.width <= 800
        assert rendered.image.width >= 760
        assert rendered.image.height > 600
        assert rendered.render_scale > 0
        whole_page = service.render_page(
            path,
            1,
            800,
            600,
            zoom=1.0,
            fit_mode="page",
        )
        assert whole_page.image.width <= 800
        assert whole_page.image.height <= 600

        bbox = (0.1, 0.2, 0.9, 0.8)
        assert service.bbox_for_view(
            bbox,
            source_region="full",
            original_view=False,
        ) == bbox
        right = service.bbox_for_view(
            bbox,
            source_region="right",
            original_view=True,
            split_ratio=0.5,
        )
        assert right == (0.55, 0.2, 0.95, 0.8)
        left = service.bbox_for_view(
            bbox,
            source_region="left",
            original_view=True,
            split_ratio=0.5,
        )
        assert left == (0.05, 0.2, 0.45, 0.8)
        canvas = service.bbox_on_canvas(
            bbox,
            image_width=1000,
            image_height=500,
            offset_x=10,
            offset_y=20,
            source_region="right",
            original_view=True,
        )
        assert canvas == (560.0, 120.0, 960.0, 420.0)
        assert service.clamp_zoom(0.1) == service.MIN_ZOOM
        assert service.clamp_zoom(99) == service.MAX_ZOOM

    print("MOFA PDF viewer checks passed: render, split-page mapping, and canvas bbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
