#!/usr/bin/env python3
"""Alice local long-term memory store.

The memory layer is intentionally deterministic and auditable.  It stores raw
turns separately from durable memories, so Alice can learn patterns without
blindly treating every chat message as a fact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB = Path(os.environ.get("ALICE_MEMORY_DB", "~/.alice/memory/alice-memory.sqlite")).expanduser()
SENSITIVE_PATTERNS = [
    re.compile(r"\b(?:password|passwd|pwd|密碼|信用卡|卡號|cvv|cvc|token|secret|api[_ -]?key)\b", re.I),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
]
TOPIC_PATTERNS = {
    "calendar": re.compile(r"行事曆|日曆|會議|開會|約|提醒|schedule|calendar|meeting", re.I),
    "file": re.compile(r"檔案|文件|雲端|drive|pdf|excel|合約|契約|摘要|資料夾", re.I),
    "ocr": re.compile(r"圖片|照片|考卷|OCR|辨識|掃描|截圖", re.I),
    "shopping": re.compile(r"購買|買|訂購|下單|付款|刷卡|衛生紙|採購", re.I),
    "travel": re.compile(r"高鐵|台鐵|機票|訂票|班次|南港|台中|台北", re.I),
    "payroll": re.compile(r"薪水|薪資|出勤|加班|勞保|健保|勞退|伙食費", re.I),
    "law": re.compile(r"法律|法規|法條|民法|刑法|勞基法|公司法", re.I),
    "engineering": re.compile(r"計算|數學|微分|積分|矩陣|工程|方程", re.I),
}
URL_PATTERN = re.compile(r"https?://[^\s<>()\"']+", re.I)
SHOPPING_URL_PATTERN = re.compile(
    r"https?://[^\s<>()\"']*(?:shopee|shp\.ee|ruten|momo|momoshop|coupang|pchome|rakuten|yahoo|asus)[^\s<>()\"']*",
    re.I,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def has_sensitive_text(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in SENSITIVE_PATTERNS)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


@dataclass
class ExtractedMemory:
    memory_type: str
    scope: str
    title: str
    content: str
    confidence: float
    source: str


class AliceMemoryStore:
    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id TEXT PRIMARY KEY,
              display_name TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turns (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('user','assistant','system','tool')),
              content TEXT NOT NULL,
              topic TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memories (
              memory_id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              scope TEXT NOT NULL DEFAULT 'user',
              memory_type TEXT NOT NULL,
              title TEXT NOT NULL,
              content TEXT NOT NULL,
              confidence REAL NOT NULL DEFAULT 0.5,
              source TEXT NOT NULL DEFAULT 'manual',
              source_turn_id INTEGER,
              usage_count INTEGER NOT NULL DEFAULT 0,
              last_used_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS memory_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              memory_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              old_json TEXT,
              new_json TEXT,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_patterns (
              user_id TEXT NOT NULL,
              pattern_key TEXT NOT NULL,
              pattern_value TEXT NOT NULL,
              count INTEGER NOT NULL DEFAULT 0,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              PRIMARY KEY (user_id, pattern_key, pattern_value)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
              content,
              user_id UNINDEXED,
              conversation_id UNINDEXED,
              topic UNINDEXED,
              content='turns',
              content_rowid='id'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
              title,
              content,
              user_id UNINDEXED,
              memory_type UNINDEXED,
              content='memories',
              content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
              INSERT INTO turns_fts(rowid, content, user_id, conversation_id, topic)
              VALUES (new.id, new.content, new.user_id, new.conversation_id, COALESCE(new.topic, ''));
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
              INSERT INTO memories_fts(rowid, title, content, user_id, memory_type)
              VALUES (new.rowid, new.title, new.content, new.user_id, new.memory_type);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
              INSERT INTO memories_fts(memories_fts, rowid, title, content, user_id, memory_type)
              VALUES('delete', old.rowid, old.title, old.content, old.user_id, old.memory_type);
              INSERT INTO memories_fts(rowid, title, content, user_id, memory_type)
              VALUES (new.rowid, new.title, new.content, new.user_id, new.memory_type);
            END;
            """
        )
        self.conn.commit()

    def ensure_user(self, user_id: str, display_name: str | None = None) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO users(user_id, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              display_name=COALESCE(excluded.display_name, users.display_name),
              updated_at=excluded.updated_at
            """,
            (user_id, display_name, ts, ts),
        )

    def record_turn(
        self,
        user_id: str,
        role: str,
        content: str,
        conversation_id: str | None = None,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        auto_extract: bool = True,
    ) -> dict[str, Any]:
        text = normalize_text(content)
        if not text:
            return {"ok": False, "error": "empty_content"}
        if role not in {"user", "assistant", "system", "tool"}:
            return {"ok": False, "error": "invalid_role"}
        conversation_id = conversation_id or user_id
        topic = infer_topic(text)
        self.ensure_user(user_id, display_name)
        cur = self.conn.execute(
            """
            INSERT INTO turns(user_id, conversation_id, role, content, topic, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, conversation_id, role, text, topic, json.dumps(metadata or {}, ensure_ascii=False), now_iso()),
        )
        turn_id = int(cur.lastrowid)
        self._record_patterns(user_id, text, topic)
        extracted: list[dict[str, Any]] = []
        if auto_extract and role == "user" and not has_sensitive_text(text):
            for memory in extract_memories(text):
                saved = self.upsert_memory(user_id=user_id, memory=memory, source_turn_id=turn_id)
                extracted.append(saved)
        self.conn.commit()
        return {"ok": True, "turn_id": turn_id, "topic": topic, "extracted": extracted}

    def upsert_memory(self, user_id: str, memory: ExtractedMemory, source_turn_id: int | None = None) -> dict[str, Any]:
        memory_id = stable_id(user_id, memory.scope, memory.memory_type, memory.title)
        ts = now_iso()
        existing = self.conn.execute(
            "SELECT * FROM memories WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if existing:
            old = dict(existing)
            confidence = max(float(existing["confidence"]), memory.confidence)
            content = memory.content if len(memory.content) >= len(existing["content"]) else existing["content"]
            self.conn.execute(
                """
                UPDATE memories
                SET content=?, confidence=?, source=?, source_turn_id=COALESCE(?, source_turn_id),
                    updated_at=?, deleted_at=NULL
                WHERE memory_id=?
                """,
                (content, confidence, memory.source, source_turn_id, ts, memory_id),
            )
            event_type = "update"
            new_payload = {"content": content, "confidence": confidence}
        else:
            self.conn.execute(
                """
                INSERT INTO memories(memory_id, user_id, scope, memory_type, title, content, confidence,
                                     source, source_turn_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    user_id,
                    memory.scope,
                    memory.memory_type,
                    memory.title,
                    memory.content,
                    memory.confidence,
                    memory.source,
                    source_turn_id,
                    ts,
                    ts,
                ),
            )
            event_type = "create"
            old = None
            new_payload = memory.__dict__ | {"memory_id": memory_id}
        self.conn.execute(
            """
            INSERT INTO memory_events(memory_id, event_type, old_json, new_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                event_type,
                json.dumps(old, ensure_ascii=False) if old else None,
                json.dumps(new_payload, ensure_ascii=False),
                ts,
            ),
        )
        return {"memory_id": memory_id, "event": event_type, "title": memory.title, "type": memory.memory_type}

    def remember(
        self,
        user_id: str,
        content: str,
        memory_type: str = "preference",
        title: str | None = None,
        scope: str = "user",
        confidence: float = 0.9,
        source: str = "manual",
    ) -> dict[str, Any]:
        text = normalize_text(content)
        if not text:
            return {"ok": False, "error": "empty_content"}
        if has_sensitive_text(text):
            return {"ok": False, "error": "sensitive_memory_rejected"}
        memory = ExtractedMemory(
            memory_type=memory_type,
            scope=scope,
            title=title or derive_title(text),
            content=text,
            confidence=confidence,
            source=source,
        )
        self.ensure_user(user_id)
        saved = self.upsert_memory(user_id, memory)
        self.conn.commit()
        return {"ok": True, **saved}

    def recall(self, user_id: str, query: str, limit: int = 8) -> dict[str, Any]:
        query = normalize_text(query)
        rows: list[sqlite3.Row]
        if query:
            fts_query = make_fts_query(query)
            try:
                rows = self.conn.execute(
                    """
                    SELECT m.*, bm25(memories_fts) AS rank
                    FROM memories_fts
                    JOIN memories m ON m.rowid = memories_fts.rowid
                    WHERE memories_fts MATCH ? AND m.user_id=? AND m.deleted_at IS NULL
                    ORDER BY rank, m.confidence DESC, m.updated_at DESC
                    LIMIT ?
                    """,
                    (fts_query, user_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = self._fallback_recall(user_id, query, limit)
            if not rows:
                rows = self._fallback_recall(user_id, query, limit)
        else:
            rows = self.conn.execute(
                """
                SELECT *, 0.0 AS rank
                FROM memories
                WHERE user_id=? AND deleted_at IS NULL
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        ids = [row["memory_id"] for row in rows]
        if ids:
            self.conn.executemany(
                "UPDATE memories SET usage_count=usage_count+1, last_used_at=? WHERE memory_id=?",
                [(now_iso(), memory_id) for memory_id in ids],
            )
            self.conn.commit()
        return {"ok": True, "memories": [public_memory(row) for row in rows]}

    def _fallback_recall(self, user_id: str, query: str, limit: int) -> list[sqlite3.Row]:
        words = [w for w in re.split(r"\W+", query) if w]
        if not words:
            return []
        clauses = " OR ".join(["content LIKE ? OR title LIKE ?" for _ in words])
        params: list[Any] = []
        for word in words:
            params.extend([f"%{word}%", f"%{word}%"])
        return self.conn.execute(
            f"""
            SELECT *, 0.0 AS rank
            FROM memories
            WHERE user_id=? AND deleted_at IS NULL AND ({clauses})
            ORDER BY confidence DESC, updated_at DESC
            LIMIT ?
            """,
            [user_id, *params, limit],
        ).fetchall()

    def context(self, user_id: str, query: str = "", limit: int = 6) -> dict[str, Any]:
        memories = self.recall(user_id, query, limit=limit)["memories"]
        turns = self.recall_turns(user_id, query, limit=limit)
        patterns = self.conn.execute(
            """
            SELECT pattern_key, pattern_value, count, last_seen_at
            FROM user_patterns
            WHERE user_id=?
            ORDER BY count DESC, last_seen_at DESC
            LIMIT 10
            """,
            (user_id,),
        ).fetchall()
        recent_topics = self.conn.execute(
            """
            SELECT topic, COUNT(*) AS count
            FROM turns
            WHERE user_id=? AND role='user' AND topic IS NOT NULL
            GROUP BY topic
            ORDER BY count DESC
            LIMIT 8
            """,
            (user_id,),
        ).fetchall()
        lines = []
        if memories:
            lines.append("Alice 長期記憶（只作為理解使用者偏好與需求 pattern，不可當作未經確認的事實）：")
            for item in memories:
                lines.append(f"- [{item['type']}] {item['title']}：{item['content']}")
        if turns:
            lines.append("相關歷史對話（真實紀錄，使用者要求回頭看/貼過/之前連結時必須優先使用）：")
            for row in turns:
                content = normalize_text(row["content"])[:700]
                lines.append(f"- {row['created_at']} {row['role']}：{content}")
        if patterns:
            lines.append("使用者近期常見需求 pattern：")
            for row in patterns[:6]:
                lines.append(f"- {row['pattern_key']}={row['pattern_value']} 出現 {row['count']} 次")
        if recent_topics:
            topic_text = "、".join(f"{row['topic']}({row['count']})" for row in recent_topics)
            lines.append(f"常見主題：{topic_text}")
        return {"ok": True, "context": "\n".join(lines), "memories": memories, "turns": [dict(row) for row in turns]}

    def recall_turns(self, user_id: str, query: str = "", limit: int = 6) -> list[sqlite3.Row]:
        query = normalize_text(query)
        url_terms = URL_PATTERN.findall(query)
        shopping_query = bool(re.search(r"蝦皮|shopee|shp\.ee|連結|網址|貼過|回頭|之前", query, re.I))
        params: list[Any] = [user_id]
        clauses: list[str] = []
        if query:
            words = [w for w in re.split(r"[\s,，。！？:：;；/\\()（）]+", query) if len(w) >= 2][:8]
            for word in words:
                clauses.append("content LIKE ?")
                params.append(f"%{word}%")
        for url in url_terms[:4]:
            clauses.append("content LIKE ?")
            params.append(f"%{url[:120]}%")
        if shopping_query:
            clauses.append("(content LIKE '%shopee%' OR content LIKE '%shp.ee%' OR content LIKE '%蝦皮%')")
        if not clauses:
            return []
        sql = f"""
            SELECT id, user_id, conversation_id, role, content, topic, created_at
            FROM turns
            WHERE user_id=? AND ({' OR '.join(clauses)})
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def delete(self, user_id: str, memory_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM memories WHERE memory_id=? AND user_id=? AND deleted_at IS NULL",
            (memory_id, user_id),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "memory_not_found"}
        ts = now_iso()
        self.conn.execute("UPDATE memories SET deleted_at=?, updated_at=? WHERE memory_id=?", (ts, ts, memory_id))
        self.conn.execute(
            "INSERT INTO memory_events(memory_id, event_type, old_json, new_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (memory_id, "delete", json.dumps(dict(row), ensure_ascii=False), None, ts),
        )
        self.conn.commit()
        return {"ok": True, "memory_id": memory_id, "deleted": True}

    def _record_patterns(self, user_id: str, text: str, topic: str | None) -> None:
        ts = now_iso()
        entries: list[tuple[str, str]] = []
        if topic:
            entries.append(("topic", topic))
        if re.search(r"快|太慢|多久|馬上|立刻|等很久", text):
            entries.append(("need", "speed"))
        if re.search(r"不要.*猜|不應該預設|先辨識|先確認|確認", text):
            entries.append(("need", "confirm_before_assume"))
        if re.search(r"報告|完整|仔細|code|程式|掃", text, re.I):
            entries.append(("style", "detailed_report"))
        if re.search(r"講中文|繁體中文|中文", text):
            entries.append(("language", "zh-TW"))
        for key, value in entries:
            self.conn.execute(
                """
                INSERT INTO user_patterns(user_id, pattern_key, pattern_value, count, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id, pattern_key, pattern_value) DO UPDATE SET
                  count=count+1,
                  last_seen_at=excluded.last_seen_at
                """,
                (user_id, key, value, ts, ts),
            )


def infer_topic(text: str) -> str | None:
    hits = [name for name, pattern in TOPIC_PATTERNS.items() if pattern.search(text)]
    return hits[0] if hits else None


def derive_title(text: str) -> str:
    text = normalize_text(text)
    return text[:32] + ("..." if len(text) > 32 else "")


def extract_memories(text: str) -> list[ExtractedMemory]:
    text = normalize_text(text)
    memories: list[ExtractedMemory] = []
    shopping_urls = SHOPPING_URL_PATTERN.findall(text)
    if shopping_urls:
        for url in shopping_urls[:6]:
            memories.append(
                ExtractedMemory(
                    memory_type="shopping_link",
                    scope="user",
                    title=derive_title(url),
                    content=url[:1000],
                    confidence=0.95,
                    source="auto_url_extract",
                )
            )
    rules = [
        (r"(?:記住|以後請記得|以後都|下次都|之後都)[：:，,\s]*(.+)", "preference", 0.88),
        (r"(?:我偏好|我喜歡|我習慣)[：:，,\s]*(.+)", "preference", 0.82),
        (r"(?:不要再|不要每次|不要老是)[：:，,\s]*(.+)", "preference", 0.8),
        (r"(?:我們公司|公司這邊|辦公室)[：:，,\s]*(.+)", "company_rule", 0.68),
        (r"(?:我的需求是|我要的是|重點是)[：:，,\s]*(.+)", "preference", 0.76),
    ]
    for pattern, memory_type, confidence in rules:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        content = normalize_text(match.group(1))
        if len(content) < 4 or has_sensitive_text(content):
            continue
        memories.append(
            ExtractedMemory(
                memory_type=memory_type,
                scope="user" if memory_type != "company_rule" else "company",
                title=derive_title(content),
                content=content[:800],
                confidence=confidence,
                source="auto_extract",
            )
        )
    return memories[:4]


def make_fts_query(query: str) -> str:
    terms = [t for t in re.split(r"[\s,，。！？:：;；/\\()（）]+", query) if t]
    safe = []
    for term in terms[:8]:
        cleaned = re.sub(r'["*]', "", term)
        if cleaned:
            safe.append(f'"{cleaned}"')
    return " OR ".join(safe) if safe else '""'


def public_memory(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "memory_id": row["memory_id"],
        "scope": row["scope"],
        "type": row["memory_type"],
        "title": row["title"],
        "content": row["content"],
        "confidence": row["confidence"],
        "source": row["source"],
        "usage_count": row["usage_count"],
        "updated_at": row["updated_at"],
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=None, separators=(",", ":")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alice local long-term memory")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite memory DB path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")

    p = sub.add_parser("record-turn")
    p.add_argument("--user-id", required=True)
    p.add_argument("--conversation-id")
    p.add_argument("--role", choices=["user", "assistant", "system", "tool"], required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--display-name")
    p.add_argument("--metadata-json", default="{}")
    p.add_argument("--no-auto-extract", action="store_true")

    p = sub.add_parser("remember")
    p.add_argument("--user-id", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--type", default="preference")
    p.add_argument("--title")
    p.add_argument("--scope", default="user")
    p.add_argument("--confidence", type=float, default=0.9)

    p = sub.add_parser("recall")
    p.add_argument("--user-id", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--limit", type=int, default=8)

    p = sub.add_parser("context")
    p.add_argument("--user-id", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--limit", type=int, default=6)

    p = sub.add_parser("delete")
    p.add_argument("--user-id", required=True)
    p.add_argument("--memory-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = AliceMemoryStore(Path(args.db))
    try:
        if args.cmd == "init":
            print_json({"ok": True, "db": str(store.db_path)})
        elif args.cmd == "record-turn":
            try:
                metadata = json.loads(args.metadata_json or "{}")
            except json.JSONDecodeError as exc:
                print_json({"ok": False, "error": f"invalid_metadata_json:{exc}"})
                return 2
            print_json(
                store.record_turn(
                    user_id=args.user_id,
                    role=args.role,
                    content=args.text,
                    conversation_id=args.conversation_id,
                    display_name=args.display_name,
                    metadata=metadata,
                    auto_extract=not args.no_auto_extract,
                )
            )
        elif args.cmd == "remember":
            print_json(
                store.remember(
                    user_id=args.user_id,
                    content=args.text,
                    memory_type=args.type,
                    title=args.title,
                    scope=args.scope,
                    confidence=args.confidence,
                )
            )
        elif args.cmd == "recall":
            print_json(store.recall(args.user_id, args.query, args.limit))
        elif args.cmd == "context":
            print_json(store.context(args.user_id, args.query, args.limit))
        elif args.cmd == "delete":
            print_json(store.delete(args.user_id, args.memory_id))
        else:
            print_json({"ok": False, "error": "unknown_command"})
            return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
