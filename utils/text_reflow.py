"""将 OCR 逐行文本重排为可读段落（仅用于导出，不修改缓存）。"""

from __future__ import annotations

import re

# 强句末：上一行以这些结尾时，通常不强行与下一行合并（除非下一行明显续写）
_STRONG_END_CHARS = "。！？!?…"
# 弱连接：行末为此类符号时，下一行高概率为同一句续行
_WEAK_JOIN_END_CHARS = "、，,：:；;—－-·"
# 闭引号结尾且前字符为句末 → 视为段落句末
_CLOSE_QUOTES = "」』】)>》〉"

_RE_BLOCK_START = re.compile(
    r"^[\s　]*(?:"
    r"[（(]?[一二三四五六七八九十百千万\d]+[）)]?[、．.]"  # 一、 （一）
    r"|[【〔「『<〈《]"  # 侧记 / 引文起
    r"|第[一二三四五六七八九十百千万\d]+"  # 第一条
    r")"
)
_RE_LATIN_TAIL = re.compile(r"[A-Za-z0-9]$")
_RE_LATIN_HEAD = re.compile(r"^[A-Za-z0-9]")


def _normalize_line(line: str) -> str:
    return line.rstrip("\r")


def _is_blank(line: str) -> bool:
    return not _normalize_line(line).strip()


def _ends_strong_sentence(line: str) -> bool:
    s = _normalize_line(line).rstrip()
    if not s:
        return False
    if s[-1] in _STRONG_END_CHARS:
        return True
    if s[-1] in _CLOSE_QUOTES and len(s) >= 2 and s[-2] in _STRONG_END_CHARS:
        return True
    return False


def _ends_weak_join(line: str) -> bool:
    s = _normalize_line(line).rstrip()
    return bool(s) and s[-1] in _WEAK_JOIN_END_CHARS


def _starts_new_block(line: str, *, prev_line: str | None = None) -> bool:
    s = _normalize_line(line)
    if not s.strip():
        return True
    if _RE_BLOCK_START.match(s):
        return True
    # 悬挂缩进：仅在前一行已句末时视为新段，否则按 OCR 软换行续写
    if re.match(r"^[\s　]{2,}\S", s):
        if prev_line and not _ends_strong_sentence(prev_line):
            return False
        return True
    return False


def _should_merge_lines(prev: str, nxt: str) -> bool:
    """判断两行是否属于同一段落的软换行。"""
    p = _normalize_line(prev)
    n = _normalize_line(nxt)
    if not p or not n:
        return False
    if _starts_new_block(n, prev_line=p):
        return False
    # 上一行未句末，或仅为弱连接符结尾 → 必合并
    if not _ends_strong_sentence(p) or _ends_weak_join(p):
        return True
    # 上一行已句末：下一行若以小写字母或接续词开头，仍可合并（西文折行）
    if _RE_LATIN_HEAD.match(n.lstrip()) and _RE_LATIN_TAIL.search(p):
        return n[0].islower()
    # 默认：句末后开启新段（避免把独立短句粘成一行）
    return False


def _needs_ascii_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return bool(_RE_LATIN_TAIL.search(left) and _RE_LATIN_HEAD.match(right.lstrip()))


def _join_line_group(lines: list[str]) -> str:
    if not lines:
        return ""
    parts: list[str] = []
    for raw in lines:
        piece = _normalize_line(raw).strip()
        if not piece:
            continue
        if not parts:
            parts.append(piece)
            continue
        prev = parts[-1]
        # 行末连字符（西文换行）
        if prev.endswith("-") and _RE_LATIN_HEAD.match(piece):
            parts[-1] = prev[:-1] + piece
            continue
        if _needs_ascii_space(prev, piece):
            parts[-1] = prev + " " + piece
        else:
            parts[-1] = prev + piece
    return parts[0] if parts else ""


def reflow_lines_to_paragraphs(lines: list[str]) -> list[str]:
    """把逐行 OCR 文本重排为段落列表。

    规则优先级（精度优先）：
    1. 空行 => 硬分段；
    2. 软换行（行末非强句末，或弱连接符结尾）=> 合并到同段；
    3. 新段起始特征（一、 / 第… / 【 / 深缩进）=> 开新段；
    4. 强句末（。！？等）=> 默认开启新段，除非下一行明显续写。
    """
    paragraphs: list[str] = []
    bucket: list[str] = []

    def flush() -> None:
        nonlocal bucket
        if not bucket:
            return
        merged = _join_line_group(bucket)
        if merged.strip():
            paragraphs.append(merged)
        bucket = []

    for line in lines:
        if _is_blank(line):
            flush()
            continue

        if not bucket:
            bucket.append(line)
            continue

        if _should_merge_lines(bucket[-1], line):
            bucket.append(line)
        else:
            flush()
            bucket = [line]

    flush()
    return paragraphs


def reflow_page_text(page_text: str) -> list[str]:
    """将单页文本重排为段落；保留原文空行语义。"""
    if not (page_text or "").strip():
        return []
    return reflow_lines_to_paragraphs(page_text.splitlines())


def looks_like_structured_payload(text: str) -> bool:
    """JSON 等结构化页面不做重排，避免破坏分析结果导出。"""
    t = (text or "").lstrip()
    return t.startswith("{") or t.startswith("[")
