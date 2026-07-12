"""JACAR 标准 PDF 文件名的解析与拼装（与 core_scraper / analysis_screen 一致）。"""
from __future__ import annotations

import re
from dataclasses import dataclass

_JACAR_REF_RE = re.compile(r"[ABCL][0-9]{11}", re.IGNORECASE)

_JACAR_FILENAME_RE = re.compile(
    r"^(?P<level2>.*?)：「(?P<title>.*?)」、JACAR Ref\.\s*(?P<ref>.*?)（(?P<image_range>.*?)）、"
    r"『(?P<parent>.*?)』[（\(](?P<repo>.*?)[）\)]$",
    re.DOTALL,
)

_INVALID_FN_CHARS = re.compile(r'[\\/:*?"<>|]')
_FN_CHAR_MAP = {
    "\\": "＼",
    "/": "／",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "\u201d",
    "<": "＜",
    ">": "＞",
    "|": "｜",
}


@dataclass(frozen=True)
class JacarFilenameParts:
    level2: str
    title: str
    ref: str
    image_range: str
    parent: str
    repo: str

    def normalized_ref(self) -> str:
        return (self.ref or "").strip().upper()

    def build_basename(self) -> str:
        ref = (self.ref or "").strip()
        image_range = (self.image_range or "").strip() or "第（）—（）画像目"
        return (
            f"{self.level2}：「{self.title}」、JACAR Ref. {ref}"
            f"（{image_range}）、『{self.parent}』（{self.repo}）"
        )

    def build_pdf_filename(self) -> str:
        return sanitize_jacar_filename(self.build_basename()) + ".pdf"


def sanitize_jacar_filename(name: str) -> str:
    """替换非法路径字符，与 core_scraper._sanitize_filename 行为一致。"""
    return _INVALID_FN_CHARS.sub(lambda m: _FN_CHAR_MAP[m.group()], (name or "")).strip(" .")


def parse_jacar_pdf_filename(path_or_name: str) -> JacarFilenameParts | None:
    """从 PDF 路径或文件名（可含 .pdf）解析标准格式。"""
    name = path_or_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    m = _JACAR_FILENAME_RE.match(name.strip())
    if not m:
        return None
    g = m.groupdict()
    return JacarFilenameParts(
        level2=g["level2"],
        title=g["title"],
        ref=g["ref"].strip(),
        image_range=g["image_range"],
        parent=g["parent"],
        repo=g["repo"],
    )


def refs_equal(a: str, b: str) -> bool:
    return (a or "").strip().upper() == (b or "").strip().upper()


def extract_jacar_ref_from_path(path: str) -> str:
    """从路径或文件名中提取 JACAR 编号（如 B03041041300）。"""
    name = str(path or "")
    match = _JACAR_REF_RE.search(name)
    return match.group(0).upper() if match else ""
