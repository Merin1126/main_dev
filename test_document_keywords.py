from __future__ import annotations

import os
import tempfile

from services.db_service import DbService


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "test.sqlite3"))
        document_id = db.upsert_document(
            source="jacar",
            native_id="B03030289700",
            title="当地反帝国主義運動状況報告ノ件",
            search_keyword="反帝國主義",
            status="downloaded",
        )
        db.add_document_keyword("jacar", "B03030289700", "中国共産党")
        db.add_document_keyword("jacar", "B03030289700", "反帝國主義")
        assert document_id == "jacar:B03030289700"
        assert db.list_document_keywords(document_id) == ["中国共産党", "反帝國主義"]
        assert db.fetchone("SELECT COUNT(*) AS n FROM documents")["n"] == 1
        db.close()
    print("Phase 5B-0 checks passed: document identity deduplication and multi-keyword mapping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
