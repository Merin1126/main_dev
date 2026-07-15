"""Normalize MOFA book-spread scans into one printed page per PDF page."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import fitz

from services.mofa_filename_service import (
    build_mineru_chunk_filename_from_pdf,
    build_mineru_input_filename_from_pdf,
)


SPLIT_SCHEMA_VERSION = 1
DEFAULT_SPLIT_RATIO = 0.5
DEFAULT_LANDSCAPE_THRESHOLD = 1.15
SPLIT_REL_DIR = os.path.join("mineru", "input")
LEGACY_SPLIT_FILENAME = "document.single-pages.pdf"
SPLIT_MANIFEST_FILENAME = "split_manifest.json"
CHUNK_SCHEMA_VERSION = 1
CHUNK_MANIFEST_FILENAME = "chunk_manifest.json"
CHUNKS_DIRNAME = "chunks"
DEFAULT_MINERU_MAX_PAGES = 200
_CHUNK_RANGE_RE = re.compile(r"(?:^|[ _-])p(\d+)-p(\d+)(?:\D|$)", re.IGNORECASE)


@dataclass(frozen=True)
class MofaSplitPageMapping:
    source_pdf_page: int
    output_pdf_pages: tuple[int, ...]
    action: str
    order: tuple[str, ...]


@dataclass(frozen=True)
class MofaPdfSplitResult:
    source_path: str
    output_path: str
    manifest_path: str
    source_pages: int
    output_pages: int
    split_pages: int
    preserved_pages: int
    split_ratio: float
    reading_order: str
    source_sha256: str
    output_sha256: str
    mappings: tuple[MofaSplitPageMapping, ...]


@dataclass(frozen=True)
class MofaMineruInputPart:
    path: str
    sha256: str
    index: int
    count: int
    start_page: int
    end_page: int
    page_count: int
    total_pages: int

    @property
    def range_label(self) -> str:
        width = max(4, len(str(self.total_pages)))
        return f"p{self.start_page:0{width}d}-p{self.end_page:0{width}d}"


@dataclass(frozen=True)
class MofaMineruChunkPlan:
    input_path: str
    manifest_path: str
    input_sha256: str
    total_pages: int
    max_pages_per_part: int
    parts: tuple[MofaMineruInputPart, ...]


class MofaPdfSplitService:
    """Create a MinerU input PDF without mutating the archival source PDF.

    Landscape source pages are treated as two-page Japanese book spreads and
    emitted right half first, then left half. Portrait pages are copied as-is.
    """

    def __init__(
        self,
        *,
        split_ratio: float = DEFAULT_SPLIT_RATIO,
        landscape_threshold: float = DEFAULT_LANDSCAPE_THRESHOLD,
        max_mineru_pages: int = DEFAULT_MINERU_MAX_PAGES,
    ) -> None:
        ratio = float(split_ratio)
        if not 0.35 <= ratio <= 0.65:
            raise ValueError("split_ratio must be between 0.35 and 0.65")
        threshold = float(landscape_threshold)
        if threshold <= 1.0:
            raise ValueError("landscape_threshold must be greater than 1.0")
        max_pages = int(max_mineru_pages)
        if max_pages <= 0:
            raise ValueError("max_mineru_pages must be positive")
        self.split_ratio = ratio
        self.landscape_threshold = threshold
        self.max_mineru_pages = max_pages

    @staticmethod
    def output_path_for_bundle(bundle_dir: str, source_path: str = "") -> str:
        filename = (
            build_mineru_input_filename_from_pdf(source_path)
            if source_path
            else LEGACY_SPLIT_FILENAME
        )
        return os.path.join(os.path.abspath(bundle_dir), SPLIT_REL_DIR, filename)

    @staticmethod
    def manifest_path_for_bundle(bundle_dir: str) -> str:
        return os.path.join(
            os.path.abspath(bundle_dir), SPLIT_REL_DIR, SPLIT_MANIFEST_FILENAME
        )

    @staticmethod
    def chunk_manifest_path_for_bundle(bundle_dir: str) -> str:
        return os.path.join(
            os.path.abspath(bundle_dir), SPLIT_REL_DIR, CHUNK_MANIFEST_FILENAME
        )

    @staticmethod
    def extract_chunk_range(value: str) -> tuple[int, int] | None:
        match = _CHUNK_RANGE_RE.search(str(value or ""))
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        return (start, end) if start > 0 and end >= start else None

    @staticmethod
    def _sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def normalize_existing_output_name(
        self,
        source_path: str,
        bundle_dir: str,
    ) -> tuple[str, bool]:
        """Rename a legacy split PDF and update its manifest without re-splitting."""
        bundle_dir = os.path.abspath(bundle_dir)
        source_path = os.path.abspath(source_path)
        desired = self.output_path_for_bundle(bundle_dir, source_path)
        manifest_path = self.manifest_path_for_bundle(bundle_dir)
        manifest: dict = {}
        candidates: list[str] = []
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as stream:
                    manifest = json.load(stream)
                rel_path = str(manifest.get("output", {}).get("path") or "")
                if rel_path:
                    candidates.append(os.path.abspath(os.path.join(bundle_dir, rel_path)))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                manifest = {}
        candidates.append(
            os.path.join(bundle_dir, SPLIT_REL_DIR, LEGACY_SPLIT_FILENAME)
        )
        input_dir = os.path.join(bundle_dir, SPLIT_REL_DIR)
        if os.path.isdir(input_dir):
            pdfs = [
                os.path.join(input_dir, name)
                for name in os.listdir(input_dir)
                if name.lower().endswith(".pdf")
                and os.path.isfile(os.path.join(input_dir, name))
            ]
            if len(pdfs) == 1:
                candidates.extend(pdfs)

        current = next((path for path in candidates if os.path.isfile(path)), "")
        renamed = False
        if current and os.path.abspath(current) != os.path.abspath(desired):
            if os.path.exists(desired):
                raise FileExistsError(f"MinerU target already exists: {desired}")
            os.makedirs(os.path.dirname(desired), exist_ok=True)
            os.rename(current, desired)
            renamed = True
        elif current:
            desired = current

        if manifest and os.path.isfile(desired):
            manifest.setdefault("source", {})["path"] = os.path.relpath(
                source_path, bundle_dir
            )
            manifest.setdefault("output", {})["path"] = os.path.relpath(
                desired, bundle_dir
            )
            temp_path = manifest_path + ".part"
            with open(temp_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temp_path, manifest_path)
        chunk_manifest = self.chunk_manifest_path_for_bundle(bundle_dir)
        if os.path.isfile(chunk_manifest) and os.path.isfile(desired):
            self.ensure_chunks(desired, bundle_dir)
        return desired, renamed

    def single_page_is_current(self, source_path: str, bundle_dir: str) -> bool:
        output_path = self.output_path_for_bundle(bundle_dir, source_path)
        manifest_path = self.manifest_path_for_bundle(bundle_dir)
        if not os.path.isfile(source_path) or not os.path.isfile(output_path):
            return False
        try:
            with open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            return bool(
                manifest.get("schema_version") == SPLIT_SCHEMA_VERSION
                and manifest.get("source", {}).get("sha256") == self._sha256(source_path)
                and manifest.get("settings", {}).get("split_ratio") == self.split_ratio
                and manifest.get("settings", {}).get("landscape_threshold")
                == self.landscape_threshold
                and manifest.get("output", {}).get("sha256") == self._sha256(output_path)
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def is_current(self, source_path: str, bundle_dir: str) -> bool:
        if not self.single_page_is_current(source_path, bundle_dir):
            return False
        output_path = self.output_path_for_bundle(bundle_dir, source_path)
        return self.chunks_are_current(output_path, bundle_dir)

    @staticmethod
    def _safe_manifest_path(bundle_dir: str, relative_path: str) -> str:
        root = os.path.abspath(bundle_dir)
        path = os.path.abspath(os.path.join(root, str(relative_path or "")))
        if os.path.commonpath((root, path)) != root:
            raise ValueError("chunk manifest path escapes the document bundle")
        return path

    def load_input_parts(
        self,
        bundle_dir: str,
        input_path: str,
    ) -> tuple[MofaMineruInputPart, ...]:
        """Load declared MinerU parts, falling back to one legacy full input."""
        bundle_dir = os.path.abspath(bundle_dir)
        input_path = os.path.abspath(input_path)
        manifest_path = self.chunk_manifest_path_for_bundle(bundle_dir)
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as stream:
                    manifest = json.load(stream)
                if manifest.get("schema_version") != CHUNK_SCHEMA_VERSION:
                    raise ValueError("unsupported chunk manifest schema")
                total_pages = int(manifest["input"]["pages"])
                values = manifest.get("parts")
                if not isinstance(values, list) or not values:
                    raise ValueError("chunk manifest contains no parts")
                count = len(values)
                parts: list[MofaMineruInputPart] = []
                for value in values:
                    path = self._safe_manifest_path(bundle_dir, value["path"])
                    if not os.path.isfile(path):
                        raise FileNotFoundError(path)
                    part = MofaMineruInputPart(
                        path=path,
                        sha256=str(value.get("sha256") or ""),
                        index=int(value["index"]),
                        count=count,
                        start_page=int(value["start_page"]),
                        end_page=int(value["end_page"]),
                        page_count=int(value["pages"]),
                        total_pages=total_pages,
                    )
                    if part.page_count != part.end_page - part.start_page + 1:
                        raise ValueError("invalid page range in chunk manifest")
                    parts.append(part)
                parts.sort(key=lambda item: item.index)
                return tuple(parts)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        if not os.path.isfile(input_path):
            return ()
        try:
            document = fitz.open(input_path)
            try:
                total_pages = len(document)
            finally:
                document.close()
        except (OSError, RuntimeError, ValueError):
            return ()
        if total_pages <= 0:
            return ()
        return (
            MofaMineruInputPart(
                path=input_path,
                sha256="",
                index=1,
                count=1,
                start_page=1,
                end_page=total_pages,
                page_count=total_pages,
                total_pages=total_pages,
            ),
        )

    def chunks_are_current(self, input_path: str, bundle_dir: str) -> bool:
        manifest_path = self.chunk_manifest_path_for_bundle(bundle_dir)
        if not os.path.isfile(input_path) or not os.path.isfile(manifest_path):
            return False
        try:
            with open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            if (
                manifest.get("schema_version") != CHUNK_SCHEMA_VERSION
                or int(manifest.get("settings", {}).get("max_pages_per_part", 0))
                != self.max_mineru_pages
                or manifest.get("input", {}).get("sha256") != self._sha256(input_path)
            ):
                return False
            parts = self.load_input_parts(bundle_dir, input_path)
            if not parts:
                return False
            for part in parts:
                if (
                    part.page_count > self.max_mineru_pages
                    or not part.sha256
                    or self._sha256(part.path) != part.sha256
                ):
                    return False
                if part.count > 1:
                    expected_name = build_mineru_chunk_filename_from_pdf(
                        input_path,
                        part.start_page,
                        part.end_page,
                        page_number_width=max(4, len(str(part.total_pages))),
                    )
                    if os.path.basename(part.path) != expected_name:
                        return False
            return True
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def ensure_chunks(self, input_path: str, bundle_dir: str) -> MofaMineruChunkPlan:
        """Create <=200-page MinerU inputs while preserving the complete input PDF."""
        input_path = os.path.abspath(input_path)
        bundle_dir = os.path.abspath(bundle_dir)
        if not os.path.isfile(input_path):
            raise FileNotFoundError(input_path)
        if self.chunks_are_current(input_path, bundle_dir):
            parts = self.load_input_parts(bundle_dir, input_path)
            return MofaMineruChunkPlan(
                input_path=input_path,
                manifest_path=self.chunk_manifest_path_for_bundle(bundle_dir),
                input_sha256=self._sha256(input_path),
                total_pages=parts[0].total_pages,
                max_pages_per_part=self.max_mineru_pages,
                parts=parts,
            )

        input_sha256 = self._sha256(input_path)
        source = fitz.open(input_path)
        try:
            total_pages = len(source)
            if total_pages <= 0:
                raise ValueError("MinerU input PDF contains no pages")
            ranges = [
                (start, min(start + self.max_mineru_pages - 1, total_pages))
                for start in range(1, total_pages + 1, self.max_mineru_pages)
            ]
            count = len(ranges)
            parts: list[MofaMineruInputPart] = []
            if count == 1:
                parts.append(
                    MofaMineruInputPart(
                        path=input_path,
                        sha256=input_sha256,
                        index=1,
                        count=1,
                        start_page=1,
                        end_page=total_pages,
                        page_count=total_pages,
                        total_pages=total_pages,
                    )
                )
            else:
                chunks_dir = os.path.join(bundle_dir, SPLIT_REL_DIR, CHUNKS_DIRNAME)
                os.makedirs(chunks_dir, exist_ok=True)
                width = max(4, len(str(total_pages)))
                for index, (start, end) in enumerate(ranges, start=1):
                    filename = build_mineru_chunk_filename_from_pdf(
                        input_path,
                        start,
                        end,
                        page_number_width=width,
                    )
                    path = os.path.join(chunks_dir, filename)
                    temp_path = path + ".part"
                    try:
                        os.remove(temp_path)
                    except FileNotFoundError:
                        pass
                    target = fitz.open()
                    try:
                        target.insert_pdf(source, from_page=start - 1, to_page=end - 1)
                        target.save(temp_path, garbage=4, deflate=True)
                    except Exception:
                        try:
                            os.remove(temp_path)
                        except FileNotFoundError:
                            pass
                        raise
                    finally:
                        target.close()
                    os.replace(temp_path, path)
                    parts.append(
                        MofaMineruInputPart(
                            path=path,
                            sha256=self._sha256(path),
                            index=index,
                            count=count,
                            start_page=start,
                            end_page=end,
                            page_count=end - start + 1,
                            total_pages=total_pages,
                        )
                    )
        finally:
            source.close()

        keep_paths = {os.path.abspath(part.path) for part in parts if part.path != input_path}
        chunks_dir = os.path.join(bundle_dir, SPLIT_REL_DIR, CHUNKS_DIRNAME)
        if os.path.isdir(chunks_dir):
            for name in os.listdir(chunks_dir):
                path = os.path.abspath(os.path.join(chunks_dir, name))
                if name.lower().endswith(".pdf") and path not in keep_paths:
                    os.remove(path)

        manifest_path = self.chunk_manifest_path_for_bundle(bundle_dir)
        manifest = {
            "schema_version": CHUNK_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "mineru_page_limit_inputs",
            "input": {
                "path": os.path.relpath(input_path, bundle_dir),
                "sha256": input_sha256,
                "pages": total_pages,
            },
            "settings": {"max_pages_per_part": self.max_mineru_pages},
            "parts": [
                {
                    "index": part.index,
                    "count": part.count,
                    "start_page": part.start_page,
                    "end_page": part.end_page,
                    "pages": part.page_count,
                    "path": os.path.relpath(part.path, bundle_dir),
                    "sha256": part.sha256,
                }
                for part in parts
            ],
        }
        temp_manifest = manifest_path + ".part"
        with open(temp_manifest, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_manifest, manifest_path)
        return MofaMineruChunkPlan(
            input_path=input_path,
            manifest_path=manifest_path,
            input_sha256=input_sha256,
            total_pages=total_pages,
            max_pages_per_part=self.max_mineru_pages,
            parts=tuple(parts),
        )

    def split(self, source_path: str, bundle_dir: str) -> MofaPdfSplitResult:
        source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            raise FileNotFoundError(source_path)
        output_path = self.output_path_for_bundle(bundle_dir, source_path)
        manifest_path = self.manifest_path_for_bundle(bundle_dir)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        temp_output = output_path + ".part"
        temp_manifest = manifest_path + ".part"
        source_sha256 = self._sha256(source_path)
        mappings: list[MofaSplitPageMapping] = []
        split_pages = 0
        preserved_pages = 0

        source = fitz.open(source_path)
        output = fitz.open()
        try:
            for source_index, source_page in enumerate(source):
                rect = source_page.rect
                output_page_numbers: list[int] = []
                if rect.width / max(rect.height, 1) >= self.landscape_threshold:
                    split_x = rect.x0 + rect.width * self.split_ratio
                    clips = (
                        ("right", fitz.Rect(split_x, rect.y0, rect.x1, rect.y1)),
                        ("left", fitz.Rect(rect.x0, rect.y0, split_x, rect.y1)),
                    )
                    for side, clip in clips:
                        target = output.new_page(width=clip.width, height=clip.height)
                        target.show_pdf_page(target.rect, source, source_index, clip=clip)
                        output_page_numbers.append(target.number + 1)
                    split_pages += 1
                    action = "split"
                    order = tuple(side for side, _clip in clips)
                else:
                    target = output.new_page(width=rect.width, height=rect.height)
                    target.show_pdf_page(target.rect, source, source_index)
                    output_page_numbers.append(target.number + 1)
                    preserved_pages += 1
                    action = "preserve"
                    order = ("full",)
                mappings.append(
                    MofaSplitPageMapping(
                        source_pdf_page=source_index + 1,
                        output_pdf_pages=tuple(output_page_numbers),
                        action=action,
                        order=order,
                    )
                )
            output.save(temp_output, garbage=4, deflate=True)
        except Exception:
            for path in (temp_output, temp_manifest):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            raise
        finally:
            output.close()
            source.close()

        os.replace(temp_output, output_path)
        output_sha256 = self._sha256(output_path)
        result = MofaPdfSplitResult(
            source_path=source_path,
            output_path=output_path,
            manifest_path=manifest_path,
            source_pages=len(mappings),
            output_pages=sum(len(item.output_pdf_pages) for item in mappings),
            split_pages=split_pages,
            preserved_pages=preserved_pages,
            split_ratio=self.split_ratio,
            reading_order="right_to_left",
            source_sha256=source_sha256,
            output_sha256=output_sha256,
            mappings=tuple(mappings),
        )
        manifest = {
            "schema_version": SPLIT_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "mineru_single_printed_page_input",
            "source": {
                "path": os.path.relpath(source_path, os.path.abspath(bundle_dir)),
                "sha256": source_sha256,
                "pages": result.source_pages,
            },
            "output": {
                "path": os.path.relpath(output_path, os.path.abspath(bundle_dir)),
                "sha256": output_sha256,
                "pages": result.output_pages,
            },
            "settings": {
                "reading_order": result.reading_order,
                "split_ratio": self.split_ratio,
                "landscape_threshold": self.landscape_threshold,
                "portrait_pages": "preserve",
            },
            "summary": {
                "split_source_pages": split_pages,
                "preserved_source_pages": preserved_pages,
            },
            "page_mapping": [asdict(item) for item in mappings],
        }
        try:
            with open(temp_manifest, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temp_manifest, manifest_path)
        except Exception:
            try:
                os.remove(temp_manifest)
            except FileNotFoundError:
                pass
            raise
        self.ensure_chunks(output_path, bundle_dir)
        return result
