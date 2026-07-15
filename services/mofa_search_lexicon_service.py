"""Versioned, explainable query-expansion knowledge base for MOFA search."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Iterable

from services.db_service import DbService
from services.mofa_mineru_normalization_service import (
    OLD_STYLE_FOLD_PAIRS_V1,
    MofaMineruNormalizationService,
)


CATEGORY_GLYPH = "glyph"
CATEGORY_OCR = "ocr"
CATEGORY_ALIAS = "alias"
CATEGORY_RELATED = "related"
LEXICON_CATEGORIES = (
    CATEGORY_GLYPH,
    CATEGORY_OCR,
    CATEGORY_ALIAS,
    CATEGORY_RELATED,
)
CATEGORY_LABELS = {
    CATEGORY_GLYPH: "新旧字体",
    CATEGORY_OCR: "OCR混淆",
    CATEGORY_ALIAS: "历史术语",
    CATEGORY_RELATED: "关联概念",
    "exact": "精确命中",
}

EXPANSION_EXACT = "exact"
EXPANSION_GLYPH = "glyph"
EXPANSION_OCR = "ocr"
EXPANSION_CONCEPT = "concept"
EXPANSION_LEVELS = (
    EXPANSION_EXACT,
    EXPANSION_GLYPH,
    EXPANSION_OCR,
    EXPANSION_CONCEPT,
)
EXPANSION_LABELS = {
    EXPANSION_EXACT: "仅精确",
    EXPANSION_GLYPH: "新旧字体",
    EXPANSION_OCR: "OCR容错",
    EXPANSION_CONCEPT: "历史关联",
}
_LEVEL_CATEGORIES = {
    EXPANSION_EXACT: (),
    EXPANSION_GLYPH: (CATEGORY_GLYPH,),
    EXPANSION_OCR: (CATEGORY_GLYPH, CATEGORY_OCR),
    EXPANSION_CONCEPT: LEXICON_CATEGORIES,
}


@dataclass(frozen=True)
class MofaLexiconRule:
    rule_id: str
    category: str
    source_term: str
    target_term: str
    source_norm: str
    target_norm: str
    bidirectional: bool
    weight: float
    active: bool
    built_in: bool
    notes: str
    provenance: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MofaExpandedTerm:
    term: str
    category: str
    weight: float
    rule_ids: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)


@dataclass(frozen=True)
class MofaSearchPlan:
    query: str
    mode: str
    expansion_level: str
    lexicon_revision: int
    groups: tuple[tuple[MofaExpandedTerm, ...], ...]

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.term for group in self.groups for item in group))

    @property
    def expanded_terms(self) -> tuple[MofaExpandedTerm, ...]:
        values: dict[str, MofaExpandedTerm] = {}
        for group in self.groups:
            for item in group:
                current = values.get(item.term)
                if current is None or item.weight > current.weight:
                    values[item.term] = item
        return tuple(values.values())

    def snapshot(self) -> dict:
        return {
            "schema_version": 1,
            "query": self.query,
            "mode": self.mode,
            "expansion_level": self.expansion_level,
            "lexicon_revision": self.lexicon_revision,
            "groups": [
                [
                    {
                        "term": item.term,
                        "category": item.category,
                        "weight": item.weight,
                        "rule_ids": list(item.rule_ids),
                    }
                    for item in group
                ]
                for group in self.groups
            ],
        }


class MofaSearchLexiconService:
    """Maintain current rules and immutable full snapshots of every revision."""

    MAX_VARIANTS_PER_TERM = 16

    def __init__(self, *, db_service: DbService | None = None) -> None:
        self.db = db_service or DbService()
        self._ensure_builtins_and_revision()

    @staticmethod
    def _normalize(value: str) -> str:
        return MofaMineruNormalizationService.normalize_search_text(value)

    @staticmethod
    def _row_to_rule(row) -> MofaLexiconRule:
        return MofaLexiconRule(
            rule_id=str(row["rule_id"]),
            category=str(row["category"]),
            source_term=str(row["source_term"]),
            target_term=str(row["target_term"]),
            source_norm=str(row["source_norm"]),
            target_norm=str(row["target_norm"]),
            bidirectional=bool(row["bidirectional"]),
            weight=float(row["weight"]),
            active=bool(row["active"]),
            built_in=bool(row["built_in"]),
            notes=str(row["notes"]),
            provenance=str(row["provenance"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _ensure_builtins_and_revision(self) -> None:
        now = self.db.utc_now_iso()
        with self.db.transaction() as conn:
            for source, target in OLD_STYLE_FOLD_PAIRS_V1:
                rule_id = f"builtin-glyph-{ord(source):x}-{ord(target):x}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO mofa_search_lexicon_rules(
                        rule_id, category, source_term, target_term,
                        source_norm, target_norm, bidirectional, weight,
                        active, built_in, notes, provenance, created_at, updated_at
                    ) VALUES (?, 'glyph', ?, ?, ?, ?, 1, 1.0, 1, 1, ?, ?, ?, ?)
                    """,
                    (
                        rule_id,
                        source,
                        target,
                        self._normalize(source),
                        self._normalize(target),
                        "标准化 Generation v1 内置旧字体折叠；不可停用",
                        "HRS builtin OLD_STYLE_FOLD_PAIRS_V1",
                        now,
                        now,
                    ),
                )
            self._publish_revision_conn(conn, "初始化内置旧字体规则")

    @staticmethod
    def _rule_payload(row) -> dict:
        return {
            key: row[key]
            for key in (
                "rule_id",
                "category",
                "source_term",
                "target_term",
                "source_norm",
                "target_norm",
                "bidirectional",
                "weight",
                "active",
                "built_in",
                "notes",
                "provenance",
            )
        }

    def _publish_revision_conn(self, conn, description: str) -> int:
        rows = conn.execute(
            "SELECT * FROM mofa_search_lexicon_rules ORDER BY rule_id"
        ).fetchall()
        payloads = [self._rule_payload(row) for row in rows]
        encoded = json.dumps(
            payloads, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        content_hash = hashlib.sha256(encoded).hexdigest()
        existing = conn.execute(
            "SELECT revision FROM mofa_search_lexicon_revisions WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing:
            revision = int(existing["revision"])
        else:
            cursor = conn.execute(
                """
                INSERT INTO mofa_search_lexicon_revisions(
                    content_hash, description, rule_count, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (content_hash, description or "词库更新", len(rows), self.db.utc_now_iso()),
            )
            revision = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO mofa_search_lexicon_revision_rules(
                    revision, rule_id, category, source_term, target_term,
                    source_norm, target_norm, bidirectional, weight, active,
                    built_in, notes, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        revision,
                        row["rule_id"],
                        row["category"],
                        row["source_term"],
                        row["target_term"],
                        row["source_norm"],
                        row["target_norm"],
                        row["bidirectional"],
                        row["weight"],
                        row["active"],
                        row["built_in"],
                        row["notes"],
                        row["provenance"],
                    )
                    for row in rows
                ),
            )
        now = self.db.utc_now_iso()
        conn.execute(
            """
            INSERT INTO mofa_search_lexicon_state(state_id, current_revision, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(state_id) DO UPDATE SET
                current_revision=excluded.current_revision,
                updated_at=excluded.updated_at
            """,
            (revision, now),
        )
        return revision

    def current_revision(self) -> int:
        row = self.db.fetchone(
            "SELECT current_revision FROM mofa_search_lexicon_state WHERE state_id = 1"
        )
        return int(row["current_revision"] or 0) if row else 0

    def revision_history(self, limit: int = 100) -> list[dict]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT * FROM mofa_search_lexicon_revisions
                ORDER BY revision DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            )
        ]

    def restore_revision(self, revision: int) -> int:
        revision = int(revision)
        exists = self.db.fetchone(
            "SELECT revision FROM mofa_search_lexicon_revisions WHERE revision = ?",
            (revision,),
        )
        if not exists:
            raise ValueError(f"词库版本 r{revision} 不存在")
        snapshot = self.db.fetchall(
            """
            SELECT * FROM mofa_search_lexicon_revision_rules
            WHERE revision = ? AND built_in = 0
            ORDER BY rule_id
            """,
            (revision,),
        )
        now = self.db.utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM mofa_search_lexicon_rules WHERE built_in = 0")
            conn.executemany(
                """
                INSERT INTO mofa_search_lexicon_rules(
                    rule_id, category, source_term, target_term,
                    source_norm, target_norm, bidirectional, weight,
                    active, built_in, notes, provenance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    (
                        row["rule_id"],
                        row["category"],
                        row["source_term"],
                        row["target_term"],
                        row["source_norm"],
                        row["target_norm"],
                        row["bidirectional"],
                        row["weight"],
                        row["active"],
                        row["notes"],
                        row["provenance"],
                        now,
                        now,
                    )
                    for row in snapshot
                ),
            )
            return self._publish_revision_conn(conn, f"恢复词库版本 r{revision}")

    def list_rules(
        self,
        *,
        category: str = "",
        active: bool | None = None,
        search_text: str = "",
    ) -> list[MofaLexiconRule]:
        clauses: list[str] = []
        params: list[object] = []
        if category:
            if category not in LEXICON_CATEGORIES:
                raise ValueError(f"未知词库类别：{category}")
            clauses.append("category = ?")
            params.append(category)
        if active is not None:
            clauses.append("active = ?")
            params.append(int(active))
        needle = (search_text or "").strip()
        if needle:
            clauses.append(
                "(instr(source_term, ?) > 0 OR instr(target_term, ?) > 0 "
                "OR instr(notes, ?) > 0 OR instr(provenance, ?) > 0)"
            )
            params.extend((needle, needle, needle, needle))
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        return [
            self._row_to_rule(row)
            for row in self.db.fetchall(
                f"""
                SELECT * FROM mofa_search_lexicon_rules {where}
                ORDER BY built_in DESC, category, source_term, target_term
                """,
                params,
            )
        ]

    @staticmethod
    def _validate_rule(
        category: str,
        source_term: str,
        target_term: str,
        weight: float,
    ) -> tuple[str, str, float]:
        if category not in LEXICON_CATEGORIES:
            raise ValueError(f"未知词库类别：{category}")
        source = (source_term or "").strip()
        target = (target_term or "").strip()
        if not source or not target:
            raise ValueError("来源词和目标词都不能为空")
        value = float(weight)
        if not 0 < value <= 1:
            raise ValueError("权重必须大于 0 且不超过 1")
        return source, target, value

    def add_rule(
        self,
        category: str,
        source_term: str,
        target_term: str,
        *,
        bidirectional: bool = True,
        weight: float = 1.0,
        notes: str = "",
        provenance: str = "",
    ) -> MofaLexiconRule:
        source, target, weight = self._validate_rule(
            category, source_term, target_term, weight
        )
        source_norm = self._normalize(source)
        target_norm = self._normalize(target)
        if not source_norm or not target_norm:
            raise ValueError("规则规范化后不能为空")
        duplicate = self._find_duplicate(
            category, source_norm, target_norm, bool(bidirectional)
        )
        if duplicate:
            raise ValueError(
                f"规则已经存在：{duplicate.source_term} → {duplicate.target_term}"
            )
        now = self.db.utc_now_iso()
        rule_id = f"lex-{uuid.uuid4().hex[:24]}"
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO mofa_search_lexicon_rules(
                    rule_id, category, source_term, target_term,
                    source_norm, target_norm, bidirectional, weight,
                    active, built_in, notes, provenance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    category,
                    source,
                    target,
                    source_norm,
                    target_norm,
                    int(bool(bidirectional)),
                    weight,
                    notes or "",
                    provenance or "",
                    now,
                    now,
                ),
            )
            self._publish_revision_conn(conn, f"新增{CATEGORY_LABELS[category]}规则")
        return self.get_rule(rule_id)

    def _find_duplicate(
        self,
        category: str,
        source_norm: str,
        target_norm: str,
        bidirectional: bool,
        *,
        exclude_rule_id: str = "",
    ) -> MofaLexiconRule | None:
        rows = self.db.fetchall(
            "SELECT * FROM mofa_search_lexicon_rules WHERE category = ? AND rule_id <> ?",
            (category, exclude_rule_id),
        )
        for row in rows:
            same = row["source_norm"] == source_norm and row["target_norm"] == target_norm
            reverse = (
                bool(bidirectional)
                and bool(row["bidirectional"])
                and row["source_norm"] == target_norm
                and row["target_norm"] == source_norm
            )
            if same or reverse:
                return self._row_to_rule(row)
        return None

    def get_rule(self, rule_id: str) -> MofaLexiconRule:
        row = self.db.fetchone(
            "SELECT * FROM mofa_search_lexicon_rules WHERE rule_id = ?", (rule_id,)
        )
        if not row:
            raise ValueError("词库规则不存在")
        return self._row_to_rule(row)

    def update_rule(
        self,
        rule_id: str,
        *,
        category: str,
        source_term: str,
        target_term: str,
        bidirectional: bool,
        weight: float,
        notes: str = "",
        provenance: str = "",
    ) -> MofaLexiconRule:
        current = self.get_rule(rule_id)
        if current.built_in:
            raise ValueError("内置规范化规则不可编辑")
        source, target, weight = self._validate_rule(
            category, source_term, target_term, weight
        )
        source_norm = self._normalize(source)
        target_norm = self._normalize(target)
        duplicate = self._find_duplicate(
            category,
            source_norm,
            target_norm,
            bool(bidirectional),
            exclude_rule_id=rule_id,
        )
        if duplicate:
            raise ValueError(
                f"规则已经存在：{duplicate.source_term} → {duplicate.target_term}"
            )
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE mofa_search_lexicon_rules SET
                    category=?, source_term=?, target_term=?, source_norm=?,
                    target_norm=?, bidirectional=?, weight=?, notes=?,
                    provenance=?, updated_at=?
                WHERE rule_id=?
                """,
                (
                    category,
                    source,
                    target,
                    source_norm,
                    target_norm,
                    int(bool(bidirectional)),
                    weight,
                    notes or "",
                    provenance or "",
                    self.db.utc_now_iso(),
                    rule_id,
                ),
            )
            self._publish_revision_conn(conn, f"编辑{CATEGORY_LABELS[category]}规则")
        return self.get_rule(rule_id)

    def set_active(self, rule_id: str, active: bool) -> MofaLexiconRule:
        current = self.get_rule(rule_id)
        if current.built_in:
            raise ValueError("内置规范化规则不可停用")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE mofa_search_lexicon_rules SET active=?, updated_at=? WHERE rule_id=?",
                (int(bool(active)), self.db.utc_now_iso(), rule_id),
            )
            self._publish_revision_conn(conn, "启用词库规则" if active else "停用词库规则")
        return self.get_rule(rule_id)

    def delete_rule(self, rule_id: str) -> None:
        current = self.get_rule(rule_id)
        if current.built_in:
            raise ValueError("内置规范化规则不可删除")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM mofa_search_lexicon_rules WHERE rule_id=?", (rule_id,))
            self._publish_revision_conn(conn, "删除自定义词库规则")

    def build_plan(
        self,
        query: str,
        mode: str,
        expansion_level: str = EXPANSION_EXACT,
    ) -> MofaSearchPlan:
        if expansion_level not in EXPANSION_LEVELS:
            raise ValueError(f"未知扩展级别：{expansion_level}")
        query = (query or "").strip()
        if not query:
            raise ValueError("请输入检索词")
        raw_terms = [query] if mode == "phrase" else query.split()
        base_terms = [self._normalize(item) for item in raw_terms if self._normalize(item)]
        if not base_terms:
            raise ValueError("请输入检索词")
        allowed = _LEVEL_CATEGORIES[expansion_level]
        rules = [
            rule
            for rule in self.list_rules(active=True)
            if rule.category in allowed and not (
                rule.category == CATEGORY_GLYPH and rule.source_norm == rule.target_norm
            )
        ]
        groups = tuple(self._expand_term(term, rules) for term in base_terms)
        return MofaSearchPlan(
            query=query,
            mode=mode,
            expansion_level=expansion_level,
            lexicon_revision=self.current_revision(),
            groups=groups,
        )

    def _expand_term(
        self,
        base_term: str,
        rules: Iterable[MofaLexiconRule],
    ) -> tuple[MofaExpandedTerm, ...]:
        values: dict[str, MofaExpandedTerm] = {
            base_term: MofaExpandedTerm(base_term, "exact", 1.0, ())
        }
        queue = [base_term]
        rules = tuple(rules)
        while queue and len(values) < self.MAX_VARIANTS_PER_TERM:
            value = queue.pop(0)
            parent = values[value]
            for rule in rules:
                replacements: list[tuple[str, str]] = []
                if rule.category in {CATEGORY_GLYPH, CATEGORY_OCR}:
                    if rule.source_norm and rule.source_norm in value:
                        replacements.append(
                            (rule.source_norm, rule.target_norm)
                        )
                    if (
                        rule.bidirectional
                        and rule.target_norm
                        and rule.target_norm in value
                    ):
                        replacements.append(
                            (rule.target_norm, rule.source_norm)
                        )
                else:
                    if value == rule.source_norm:
                        replacements.append((value, rule.target_norm))
                    if rule.bidirectional and value == rule.target_norm:
                        replacements.append((value, rule.source_norm))
                for source, target in replacements:
                    candidate = value.replace(source, target)
                    if not candidate or candidate == value or candidate in values:
                        continue
                    item = MofaExpandedTerm(
                        term=candidate,
                        category=rule.category,
                        weight=min(parent.weight, rule.weight),
                        rule_ids=tuple(dict.fromkeys((*parent.rule_ids, rule.rule_id))),
                    )
                    values[candidate] = item
                    queue.append(candidate)
                    if len(values) >= self.MAX_VARIANTS_PER_TERM:
                        break
                if len(values) >= self.MAX_VARIANTS_PER_TERM:
                    break
        return tuple(values.values())

    def export_file(self, path: str) -> str:
        custom = [rule for rule in self.list_rules() if not rule.built_in]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if path.lower().endswith(".csv"):
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=(
                        "category",
                        "source_term",
                        "target_term",
                        "bidirectional",
                        "weight",
                        "active",
                        "notes",
                        "provenance",
                    ),
                )
                writer.writeheader()
                for rule in custom:
                    row = asdict(rule)
                    writer.writerow({key: row[key] for key in writer.fieldnames})
        else:
            payload = {
                "schema_version": 1,
                "lexicon_revision": self.current_revision(),
                "rules": [
                    {
                        key: value
                        for key, value in asdict(rule).items()
                        if key not in {"rule_id", "source_norm", "target_norm", "built_in", "created_at", "updated_at"}
                    }
                    for rule in custom
                ],
            }
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        return path

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"", "0", "false", "no", "否"}

    def import_file(self, path: str) -> tuple[int, int]:
        if path.lower().endswith(".csv"):
            with open(path, "r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        else:
            with open(path, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
            rows = payload.get("rules") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("JSON 词库缺少 rules 数组")
        staged: list[dict] = []
        skipped = 0
        known = [
            (rule.category, rule.source_norm, rule.target_norm, rule.bidirectional)
            for rule in self.list_rules()
        ]
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            try:
                category = str(row.get("category") or "")
                source, target, weight = self._validate_rule(
                    category,
                    str(row.get("source_term") or ""),
                    str(row.get("target_term") or ""),
                    float(row.get("weight") or 1.0),
                )
                source_norm = self._normalize(source)
                target_norm = self._normalize(target)
                bidirectional = self._as_bool(row.get("bidirectional", True))
                duplicate = any(
                    existing_category == category
                    and (
                        (existing_source == source_norm and existing_target == target_norm)
                        or (
                            bidirectional
                            and existing_bidirectional
                            and existing_source == target_norm
                            and existing_target == source_norm
                        )
                    )
                    for existing_category, existing_source, existing_target, existing_bidirectional in known
                )
                if duplicate:
                    skipped += 1
                    continue
                staged.append(
                    {
                        "rule_id": f"lex-{uuid.uuid4().hex[:24]}",
                        "category": category,
                        "source_term": source,
                        "target_term": target,
                        "source_norm": source_norm,
                        "target_norm": target_norm,
                        "bidirectional": bidirectional,
                        "weight": weight,
                        "active": self._as_bool(row.get("active", True)),
                        "notes": str(row.get("notes") or ""),
                        "provenance": str(row.get("provenance") or ""),
                    }
                )
                known.append((category, source_norm, target_norm, bidirectional))
            except (TypeError, ValueError):
                skipped += 1
        if staged:
            now = self.db.utc_now_iso()
            with self.db.transaction() as conn:
                conn.executemany(
                    """
                    INSERT INTO mofa_search_lexicon_rules(
                        rule_id, category, source_term, target_term,
                        source_norm, target_norm, bidirectional, weight,
                        active, built_in, notes, provenance, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        (
                            item["rule_id"],
                            item["category"],
                            item["source_term"],
                            item["target_term"],
                            item["source_norm"],
                            item["target_norm"],
                            int(item["bidirectional"]),
                            item["weight"],
                            int(item["active"]),
                            item["notes"],
                            item["provenance"],
                            now,
                            now,
                        )
                        for item in staged
                    ),
                )
                self._publish_revision_conn(conn, f"批量导入 {len(staged)} 条词库规则")
        return len(staged), skipped
