from __future__ import annotations

import os

from services.mofa_filename_service import (
    build_mineru_chunk_filename_from_pdf,
    build_mineru_input_filename,
    build_mofa_pdf_filename,
    extract_mofa_native_id,
)


def main() -> int:
    native_id = "MOFA_T14_22_UF09B70C4EDEF"
    title = "5 北京関税特別会議事件/附録:照会"
    pdf_name = build_mofa_pdf_filename(title, native_id)
    split_name = build_mineru_input_filename(title, native_id)
    assert pdf_name == (
        "5 北京関税特別会議事件／附録：照会 "
        "[MOFA_T14_22_UF09B70C4EDEF].pdf"
    )
    assert split_name.endswith(
        "[MOFA_T14_22_UF09B70C4EDEF] single-pages.pdf"
    )
    assert extract_mofa_native_id(split_name + "-83867cac") == native_id
    chunk_name = build_mineru_chunk_filename_from_pdf(split_name, 201, 400)
    assert chunk_name.endswith(
        "[MOFA_T14_22_UF09B70C4EDEF] single-pages p0201-p0400.pdf"
    )
    assert extract_mofa_native_id(chunk_name + "-result") == native_id
    assert len(("長" * 300 + os.extsep + "pdf").encode("utf-8")) > 240
    assert len(build_mofa_pdf_filename("長" * 300, native_id).encode("utf-8")) <= 240
    long_input = build_mineru_input_filename("長" * 300, native_id)
    long_chunk = build_mineru_chunk_filename_from_pdf(long_input, 1, 200)
    assert extract_mofa_native_id(long_chunk) == native_id
    assert len(long_chunk.encode("utf-8")) <= 240
    print("MOFA filename checks passed: readable title, chunks, stable ID, safe length.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
