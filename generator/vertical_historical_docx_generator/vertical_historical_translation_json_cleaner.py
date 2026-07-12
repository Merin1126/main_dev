#!/usr/bin/env python3
"""Clean Chinese-translation historical-record JSON/text into paragraph-flow schema JSON.

Usage:
    python vertical_historical_translation_json_cleaner.py translation.json cleaned_schema.json
    python vertical_historical_translation_json_cleaner.py translation.json cleaned_schema.json --page-index 0

The output is designed for:
    vertical_historical_translation_docx_generator.py cleaned_schema.json output.docx

This cleaner is heuristic by design: it extracts <transcription> blocks,
removes layout-analysis text and archival marks, infers basic paragraph roles,
and leaves uncertain OCR readings inside the main text for human review.
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path


SCHEMA_VERSION = "vertical_historical_translation_record_paragraph_flow_v1"


def default_hrs_db_path(project_root: str | Path | None = None) -> Path:
    if project_root:
        return Path(project_root).expanduser().resolve() / "database" / "hrs.sqlite3"
    return Path(__file__).resolve().parents[2] / "database" / "hrs.sqlite3"

ERA_DATE_RE = re.compile(r"(明治|大正|昭和|平成|令和)[一二三四五六七八九十〇零元\d]+年")
DOC_NO_RE = re.compile(r"(機密|机密|秘|第[一二三四五六七八九十〇零\d]+[号號]|[甲乙丙丁]号)")
NUMERAL_PATTERN = r"(?:[一二三四五六七八九十百千〇零壱弐参\d０-９]+)"
BRACKET_OPEN_CHARS = r"（(︵【［\[\{｛〔「『〈《〖〚〘"
BRACKET_CLOSE_CHARS = r"）)︶】］\]\}｝〕」』〉》〗〛〙"
NUMBERED_RE = re.compile(rf"(?=(?:[{BRACKET_OPEN_CHARS}]\s*{NUMERAL_PATTERN}\s*[{BRACKET_CLOSE_CHARS}]))")
NUMBERED_START_RE = re.compile(
    rf"^\s*(?:(?:[{BRACKET_OPEN_CHARS}]\s*{NUMERAL_PATTERN}\s*[{BRACKET_CLOSE_CHARS}])|(?:{NUMERAL_PATTERN}[、．.。]))"
)
WHOLE_MARK_RE = re.compile(r"^【([^】]+)】$")
FOLIO_RE_LIST = [
    re.compile(r"【(?:底注|档案编号|檔案編號)[:：]\s*([0-9０-９]{3,5})】"),
    re.compile(r"【[^】]*(?:No\.|番号|編號|编号)[^】]*?([0-9０-９]{3,5})[^】]*】"),
]
JACAR_REF_RE = re.compile(r"(?:JACAR\s+Ref\.?|JACAR|Ref\.?)\s*[:：]?\s*([A-Z]\d{10,})", re.I)


DEFAULT_LAYOUT = {
    "page": {
        "width_cm": 29.7,
        "height_cm": 14.7,
        "margins_cm": {"top": 0.55, "right": 0.75, "bottom": 1.6, "left": 0.75},
    },
    "font": "宋体",
    "body_size_pt": 10.0,
    "line_pitch": 190,
    "border": {"enabled": True, "size": 6, "space": 4, "color": "000000"},
    "normalization": {
        "fullwidth_ascii_digits": True,
        "fullwidth_ascii_letters": True,
        "fullwidth_punctuation": True,
        "vertical_parentheses": True,
        "normalize_numbered_marker": True,
    },
    "numbered_item": {"hanging_indent_chars": 2},
    "translation_merge": {
        "max_chars_per_page": 900,
        "max_source_pages_per_page": 3
    },
    "role_styles": {
        "header": {"first_line_indent_chars": 0},
        "date": {"first_line_indent_chars": 1},
        "sender": {"first_line_indent_chars": 2},
        "recipient": {"first_line_indent_chars": 2},
        "title": {
            "alignment": "center",
            "bold": True,
            "space_before_lines": 0.5,
            "space_after_lines": 0.5,
        },
    },
    "folio": {
        "placement": "bottom_center_footer_box",
        "size_pt": 8.6,
        "bold": False,
        "footer_distance_cm": 0.06,
        "space_before_pt": 0,
    },
}


def parse_raw_pages_payload(raw: str) -> tuple[list[str], dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [raw], {"input_format": "plain_text"}

    if isinstance(data, dict) and isinstance(data.get("pages"), list):
        return [str(p) for p in data["pages"]], {"input_format": data.get("format", "json_pages")}
    if isinstance(data, list):
        return [str(p) for p in data], {"input_format": "json_list"}
    return [raw], {"input_format": "json_unknown"}


def read_raw_pages(path):
    raw = Path(path).read_text(encoding="utf-8")
    return parse_raw_pages_payload(raw)


def normalize_pages_list(pages: list[str]) -> tuple[list[str], dict]:
    return [str(p or "") for p in pages], {"input_format": "memory_pages"}


def extract_transcription(page_text):
    matches = re.findall(r"<transcription>\s*(.*?)\s*</transcription>", page_text, flags=re.S)
    if matches:
        return "\n\n".join(matches)
    text = re.sub(r"<layout_analysis>.*?</layout_analysis>", "", page_text, flags=re.S)
    text = re.sub(r"</?transcription>", "", text)
    return text.strip()


def extract_folio(text, fallback=None):
    for regex in FOLIO_RE_LIST:
        found = regex.findall(text)
        if found:
            return to_halfwidth_digits(found[-1])
    if fallback is not None:
        return f"{fallback + 1:04d}"
    return ""


def extract_jacar_ref(text):
    match = JACAR_REF_RE.search(text)
    return match.group(1).upper() if match else ""


def normalize_jacar_ref(value):
    if not value:
        return ""
    value = str(value).strip()
    if value.lower().startswith("jacar:"):
        value = value.split(":", 1)[1]
    return value


def query_jacar_ref_by_cache_key(cache_key, db_path=None, *, project_root=None):
    db_path = Path(db_path) if db_path else default_hrs_db_path(project_root)
    if not cache_key or not db_path.exists():
        return ""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                """
                SELECT native_id, document_id
                FROM document_cache_index
                WHERE cache_key = ?
                   OR cache_basename = ?
                   OR cache_basename = ?
                ORDER BY
                  CASE cache_kind
                    WHEN 'ocr' THEN 0
                    WHEN 'analysis' THEN 1
                    ELSE 2
                  END
                LIMIT 1
                """,
                (cache_key, cache_key, f"{cache_key}.txt"),
            ).fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    return normalize_jacar_ref(row[0] or row[1])


def to_halfwidth_digits(text):
    return "".join(chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" else ch for ch in str(text))


def is_archival_mark(line):
    m = WHOLE_MARK_RE.match(line)
    if not m:
        return False
    payload = m.group(1)
    keywords = ("印", "底注", "REEL", "MT", "M.T.", "档案", "檔案", "受附", "受領", "送付")
    return any(k in payload for k in keywords)


def clean_line(line, keep_marks=False):
    line = line.strip().strip("\ufeff")
    line = line.replace("\u3000", " ").strip()
    if not line:
        return ""
    if line.startswith("<") and line.endswith(">"):
        return ""
    if is_archival_mark(line) and not keep_marks:
        return ""
    if not keep_marks:
        line = re.sub(r"【印[^】]*】", "", line)
        line = re.sub(r"【(?:底注|REEL|档案|檔案|受附|受領|送付)[^】]*】", "", line)
    return re.sub(r"\s+", "　", line).strip("　")


def chunk_lines(lines):
    chunks = []
    current = []
    for line in lines:
        if not line:
            if current:
                chunks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        chunks.append(current)
    return chunks


def join_chunk(lines):
    # OCR line breaks are visual; for paragraph flow they should usually be
    # removed. Spaces inside names/titles have already become ideographic spaces.
    return "".join(lines)


def infer_role(text, position, before_marker):
    compact = text.replace("　", "")
    if compact in {"記", "记", "左記", "左记", "左　記", "左　记"}:
        return "marker"
    if ERA_DATE_RE.search(compact):
        return "date"
    if compact.endswith("殿") or compact.startswith("在"):
        return "recipient"
    if any(k in compact for k in ("署長", "署长", "警部", "領事", "领事", "主任", "書記生", "书记生")) and before_marker:
        return "sender"
    if DOC_NO_RE.search(compact) and position <= 4:
        return "header"
    if compact in {"甲号", "乙号", "丙", "甲", "乙"}:
        return "header"
    if "件" in compact and before_marker and not compact.startswith(("本件", "关于本件", "關於本件")):
        return "title"
    return "body"


def split_inline_numbered_items(text):
    parts = [p for p in NUMBERED_RE.split(text) if p]
    if len(parts) <= 1:
        return [text]
    result = []
    buffer = ""
    for part in parts:
        if NUMBERED_START_RE.match(part):
            if buffer:
                result.append(buffer)
            buffer = part
        else:
            buffer += part
    if buffer:
        result.append(buffer)
    return result


def make_block(role, text):
    if role == "body" and NUMBERED_START_RE.match(text):
        role = "numbered_item"
    block = {"role": role, "text": text}
    if role == "title":
        block["bold"] = True
    if role == "body":
        block["first_line_indent_chars"] = 1
    return block


def transcription_to_paragraphs(transcription, keep_marks=False):
    raw_lines = transcription.splitlines()
    cleaned_lines = [clean_line(line, keep_marks=keep_marks) for line in raw_lines]
    chunks = chunk_lines(cleaned_lines)

    blocks = []
    seen_marker = False
    for idx, chunk in enumerate(chunks):
        work_items = [chunk]
        joined = join_chunk(chunk)
        joined_compact = joined.replace("　", "")
        if not seen_marker and len(chunk) > 1:
            headerish = any(DOC_NO_RE.search(line.replace("　", "")) or ERA_DATE_RE.search(line.replace("　", "")) for line in chunk)
            titleish = "件" in joined_compact and not joined_compact.startswith("本件")
            if headerish and not titleish:
                work_items = [[line] for line in chunk]

        for work_item in work_items:
            text = join_chunk(work_item)
            if not text:
                continue
            role = infer_role(text, idx, before_marker=not seen_marker)
            if role == "marker":
                seen_marker = True

            pieces = split_inline_numbered_items(text)
            if len(pieces) > 1:
                for piece in pieces:
                    piece_role = "body"
                    blocks.append(make_block(piece_role, piece))
                continue

            blocks.append(make_block(role, text))
        continue

    return blocks


def derive_title(paragraphs):
    titles = [p["text"] for p in paragraphs if p.get("role") == "title"]
    return "".join(titles) if titles else ""


def build_schema_from_pages(
    raw_pages: list[str],
    *,
    source_path: str,
    cache_key: str,
    page_index=None,
    keep_marks=False,
    title=None,
    db_path=None,
    project_root=None,
    jacar_ref_override: str | None = None,
    input_meta: dict | None = None,
) -> dict:
    raw_text = "\n".join(str(p or "") for p in raw_pages)
    jacar_ref = (jacar_ref_override or "").strip().upper()
    if not jacar_ref:
        jacar_ref = extract_jacar_ref(raw_text) or query_jacar_ref_by_cache_key(
            cache_key,
            db_path=db_path,
            project_root=project_root,
        )
    meta = dict(input_meta or {})
    selected: list[tuple[int, str]] = []
    if page_index is None:
        selected = list(enumerate(raw_pages))
    else:
        selected = [(page_index, raw_pages[page_index])]

    pages = []
    first_title = ""
    for output_idx, (source_idx, page_text) in enumerate(selected, start=1):
        transcription = extract_transcription(str(page_text))
        folio = extract_folio(transcription, fallback=source_idx)
        paragraphs = transcription_to_paragraphs(transcription, keep_marks=keep_marks)
        if not first_title:
            first_title = derive_title(paragraphs)
        pages.append(
            {
                "page_id": f"page_{output_idx:04d}",
                "source_page_index": source_idx,
                "source_page_label": f"page_{source_idx + 1:04d}",
                "folio": folio,
                "paragraphs": paragraphs,
            }
        )

    source = {
        "record_id": cache_key,
        "title": title or first_title,
        "translation_cache": source_path,
        "notes": [
            "Generated by vertical_historical_translation_json_cleaner.py.",
            "Review inferred roles before final publication.",
        ],
        **meta,
    }
    if jacar_ref:
        source["jacar_ref"] = jacar_ref

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "layout": DEFAULT_LAYOUT,
        "pages": pages,
    }


def build_schema(raw_path, page_index=None, keep_marks=False, record_id=None, title=None, db_path=None, project_root=None):
    raw_pages, input_meta = read_raw_pages(raw_path)
    source_path = str(Path(raw_path).expanduser())
    cache_key = record_id or Path(source_path).stem
    return build_schema_from_pages(
        raw_pages,
        source_path=source_path,
        cache_key=cache_key,
        page_index=page_index,
        keep_marks=keep_marks,
        title=title,
        db_path=db_path,
        project_root=project_root,
        input_meta=input_meta,
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Clean Chinese-translation historical-record JSON/text into paragraph-flow schema JSON."
    )
    parser.add_argument("input_raw", help="Chinese translation JSON/txt file")
    parser.add_argument("output_json", help="Cleaned schema JSON output")
    parser.add_argument("--page-index", type=int, help="Only convert one zero-based page index")
    parser.add_argument("--keep-marks", action="store_true", help="Keep archival marks such as seals/REEL lines")
    parser.add_argument("--record-id", help="Override source.record_id")
    parser.add_argument("--title", help="Override source.title")
    return parser.parse_args()


def main():
    args = parse_args()
    schema = build_schema(
        args.input_raw,
        page_index=args.page_index,
        keep_marks=args.keep_marks,
        record_id=args.record_id,
        title=args.title,
    )
    Path(args.output_json).write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
