"""Application workflow for MOFA catalog scans and optional downloads."""
from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass
from typing import Callable

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaCatalogScraper, MofaVolume
from services.db_service import DbService
from services.mofa_download_service import (
    MofaDownloadCancelled,
    MofaDownloadResult,
    MofaDownloadService,
)


MOFA_MODE_SCAN = "scan"
MOFA_MODE_MATCHED = "matched"
MOFA_MODE_ALL_CONTENT = "all_content"
MOFA_MODES = {MOFA_MODE_SCAN, MOFA_MODE_MATCHED, MOFA_MODE_ALL_CONTENT}

ProgressCallback = Callable[[float, int, str], None]
RunStartedCallback = Callable[[int], None]
CatalogReadyCallback = Callable[["MofaWorkflowResult"], None]


@dataclass(frozen=True)
class MofaWorkflowResult:
    mode: str
    keyword: str
    year_from: int
    year_to: int
    volumes: tuple[MofaVolume, ...]
    all_items: tuple[MofaCatalogItem, ...]
    matched_items: tuple[MofaCatalogItem, ...]
    selected_items: tuple[MofaCatalogItem, ...]
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    aborted: bool = False

    @property
    def content_items(self) -> tuple[MofaCatalogItem, ...]:
        return tuple(item for item in self.all_items if item.item_kind == "content")


def normalize_catalog_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    normalized = normalized.translate(
        str.maketrans(
            {
                "國": "国",
                "黨": "党",
                "產": "産",
                "會": "会",
                "聯": "連",
                "蘇": "ソ",
            }
        )
    )
    return "".join(ch for ch in normalized if ch.isalnum())


def title_matches_keyword(title: str, keyword: str) -> bool:
    needle = normalize_catalog_text(keyword)
    return bool(needle and needle in normalize_catalog_text(title))


class MofaWorkflowService:
    def __init__(
        self,
        *,
        catalog_scraper: MofaCatalogScraper | None = None,
        download_service: MofaDownloadService | None = None,
        db_service: DbService | None = None,
    ) -> None:
        self.db = db_service or DbService()
        self.catalog = catalog_scraper or MofaCatalogScraper()
        self.downloader = download_service or MofaDownloadService(db_service=self.db)

    @staticmethod
    def _validate_years(year_from: int, year_to: int) -> tuple[int, int]:
        start, end = int(year_from), int(year_to)
        if start > end:
            raise ValueError("起始年份不能晚于结束年份")
        if start < 1921 or end > 1927:
            raise ValueError("MOFA 当前研究范围限定为 1921—1927 年")
        return start, end

    def run(
        self,
        *,
        keyword: str,
        year_from: int,
        year_to: int,
        mode: str,
        stop_event: threading.Event,
        on_progress: ProgressCallback | None = None,
        on_run_started: RunStartedCallback | None = None,
        on_catalog_ready: CatalogReadyCallback | None = None,
    ) -> MofaWorkflowResult:
        if mode not in MOFA_MODES:
            raise ValueError(f"unsupported MOFA workflow mode: {mode}")
        start, end = self._validate_years(year_from, year_to)
        keyword = (keyword or "").strip()
        if mode == MOFA_MODE_MATCHED and not keyword:
            raise ValueError("下载目录标题命中项时，检索关键词不能为空")

        run_id = self.db.begin_download_run(
            keyword=keyword,
            year_from=str(start),
            year_to=str(end),
            notes=f"source=mofa; mode={mode}",
        )
        if on_run_started is not None:
            on_run_started(run_id)

        all_items: list[MofaCatalogItem] = []
        selected: list[MofaCatalogItem] = []
        downloaded = skipped = failed = 0
        aborted = False
        try:
            if on_progress is not None:
                on_progress(0, 1, "正在读取 MOFA 卷号总目录...")
            target_volumes = [
                volume
                for volume in self.catalog.discover_volumes()
                if start <= volume.gregorian_year <= end
            ]
            volumes: list[MofaVolume] = []
            for index, volume in enumerate(target_volumes, start=1):
                if stop_event.is_set():
                    aborted = True
                    break
                if on_progress is not None:
                    on_progress(
                        index - 1,
                        max(1, len(target_volumes)),
                        f"正在解析 {volume.volume_label} ({index}/{len(target_volumes)})",
                    )
                all_items.extend(self.catalog.fetch_volume_items(volume))
                volumes.append(volume)

            content_items = [item for item in all_items if item.item_kind == "content"]
            matched = [item for item in content_items if title_matches_keyword(item.title, keyword)]
            if mode == MOFA_MODE_MATCHED:
                selected = matched
            elif mode == MOFA_MODE_ALL_CONTENT:
                selected = content_items

            preview = MofaWorkflowResult(
                mode=mode,
                keyword=keyword,
                year_from=start,
                year_to=end,
                volumes=tuple(volumes),
                all_items=tuple(all_items),
                matched_items=tuple(matched),
                selected_items=tuple(selected),
                aborted=aborted,
            )
            if on_catalog_ready is not None:
                on_catalog_ready(preview)

            if not aborted and mode != MOFA_MODE_SCAN:
                for item in selected:
                    self.downloader.register_item(
                        item,
                        search_keyword=keyword or "MOFA全量",
                        run_id=run_id,
                    )
                for index, item in enumerate(selected, start=1):
                    if stop_event.is_set():
                        aborted = True
                        break
                    if on_progress is not None:
                        on_progress(
                            index - 1,
                            max(1, len(selected)),
                            f"正在下载 {item.title} ({index}/{len(selected)})",
                        )
                    try:
                        result: MofaDownloadResult = self.downloader.download_item(
                            item,
                            search_keyword=keyword or "MOFA全量",
                            run_id=run_id,
                            on_progress=(
                                (
                                    lambda current, total, item_index=index, item_title=item.title: on_progress(
                                        (item_index - 1) + (min(1.0, current / total) if total else 0.0),
                                        max(1, len(selected)),
                                        f"正在下载 {item_title} · {current}/{total or '?'} bytes",
                                    )
                                )
                                if on_progress is not None
                                else None
                            ),
                            should_stop=stop_event.is_set,
                        )
                        if result.status == "downloaded":
                            downloaded += 1
                        else:
                            skipped += 1
                    except MofaDownloadCancelled:
                        aborted = True
                        break
                    except Exception:
                        failed += 1

            result = MofaWorkflowResult(
                mode=mode,
                keyword=keyword,
                year_from=start,
                year_to=end,
                volumes=tuple(volumes),
                all_items=tuple(all_items),
                matched_items=tuple(matched),
                selected_items=tuple(selected),
                downloaded=downloaded,
                skipped=skipped,
                failed=failed,
                aborted=aborted,
            )
            completed = downloaded + skipped + failed
            self.db.finish_download_run(
                run_id,
                dispatched=len(selected),
                completed=completed,
                succeeded=downloaded + skipped,
                failed=failed,
                sidecar_only=0,
                notes=(
                    f"source=mofa; mode={mode}; volumes={len(volumes)}; "
                    f"items={len(all_items)}; matches={len(matched)}; aborted={aborted}"
                ),
            )
            return result
        except Exception as exc:
            self.db.finish_download_run(
                run_id,
                dispatched=len(selected),
                completed=downloaded + skipped + failed,
                succeeded=downloaded + skipped,
                failed=failed + 1,
                sidecar_only=0,
                notes=f"source=mofa; mode={mode}; workflow_error={exc}",
            )
            raise
