#!/usr/bin/env python3
"""Mirror and search Taiwan MOJ law data locally for Alice.

The fast path is deterministic:
1. Download official MOJ ZIP JSON files.
2. Convert laws/orders and articles into SQLite + FTS5.
3. Query local FTS before any LLM is involved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import ssl
import urllib.request
import zipfile

# law.moj.gov.tw 憑證缺少 Subject Key Identifier，Python 3.13 預設拒絕；
# 下載時跳過驗證（僅限本 mirror 功能，資料為公開政府法規）。
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_DATA_DIR = Path(os.environ.get("ALICE_TW_LAW_DATA_DIR", "/home/alice_gx10/.alice/law-data"))
DEFAULT_DB = Path(os.environ.get("ALICE_TW_LAW_DB", DEFAULT_DATA_DIR / "tw-law.sqlite"))
USER_AGENT = "AliceTaiwanLawMirror/1.0 (local legal lookup; source law.moj.gov.tw)"

ENDPOINTS = {
    "ch_law": {
        "url": "https://law.moj.gov.tw/api/ch/law/json",
        "language": "ch",
        "doc_type": "law",
        "label": "中文法律",
    },
    "ch_order": {
        "url": "https://law.moj.gov.tw/api/ch/order/json",
        "language": "ch",
        "doc_type": "order",
        "label": "中文命令",
    },
    "en_law": {
        "url": "https://law.moj.gov.tw/api/en/law/json",
        "language": "en",
        "doc_type": "law",
        "label": "英文法律",
    },
    "en_order": {
        "url": "https://law.moj.gov.tw/api/en/order/json",
        "language": "en",
        "doc_type": "order",
        "label": "英文命令",
    },
}

LAW_ALIASES = {
    "勞基法": "勞動基準法",
    "個資法": "個人資料保護法",
    "消保法": "消費者保護法",
    "智財案件審理法": "智慧財產案件審理法",
    "營業稅法": "加值型及非加值型營業稅法",
}


@dataclass
class ImportStats:
    laws: int = 0
    articles: int = 0
    source: str = ""


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}, ensure_ascii=False), flush=True)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(normalize_text(x) for x in value if normalize_text(x))
    if isinstance(value, dict):
        return "\n".join(f"{k}: {normalize_text(v)}" for k, v in value.items() if normalize_text(v))
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def pick(obj: dict[str, Any], *names: str) -> Any:
    if not isinstance(obj, dict):
        return None
    lowered = {str(k).lower(): k for k in obj}
    for name in names:
        if name in obj:
            return obj[name]
        key = lowered.get(name.lower())
        if key is not None:
            return obj[key]
    return None


def first_nonempty(obj: dict[str, Any], *names: str) -> str:
    for name in names:
        value = normalize_text(pick(obj, name))
        if value:
            return value
    return ""


def download_endpoint(key: str, info: dict[str, str], raw_dir: Path, force: bool = False) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    zip_path = raw_dir / f"{key}.zip"
    json_path = raw_dir / f"{key}.json"
    if json_path.exists() and not force:
        log("law_download_skip", source=key, json=str(json_path))
        return json_path

    tmp_path = zip_path.with_suffix(".zip.part")
    req = urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT})
    log("law_download_start", source=key, url=info["url"])
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as response, tmp_path.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    tmp_path.replace(zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise RuntimeError(f"{zip_path} has no JSON member")
        with zf.open(names[0]) as src, json_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    log("law_download_done", source=key, zip=str(zip_path), json=str(json_path), bytes=zip_path.stat().st_size)
    return json_path


def load_json(path: Path) -> Any:
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    return json.loads(text)


def likely_law_object(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = {str(k).lower() for k in obj.keys()}
    has_name = any(k in keys for k in ["lawname", "name", "法規名稱"])
    has_article = any("article" in k or "content" in k or "條文" in k for k in keys)
    return has_name and has_article


def find_law_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data):
            direct = [x for x in data if likely_law_object(x)]
            if direct:
                return direct
        found: list[dict[str, Any]] = []
        for item in data:
            found.extend(find_law_records(item))
        return found
    if isinstance(data, dict):
        for key in ["Laws", "Law", "LawList", "Orders", "Order", "OrderList", "Data", "data"]:
            value = pick(data, key)
            if isinstance(value, list):
                records = [x for x in value if likely_law_object(x)]
                if records:
                    return records
        found = []
        for value in data.values():
            found.extend(find_law_records(value))
        return found
    return []


def article_list(law: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ["LawArticles", "Articles", "Article", "LawArticle", "OrderArticles", "法規內容", "條文"]:
        value = pick(law, key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def article_no(article: dict[str, Any]) -> str:
    return first_nonempty(article, "ArticleNo", "No", "LawNo", "ArticleNumber", "條號")


def article_text(article: dict[str, Any]) -> str:
    text = first_nonempty(article, "ArticleContent", "Content", "Text", "ArticleText", "條文內容", "內容")
    if text:
        return text
    parts = []
    for key, value in article.items():
        if key.lower() not in {"articleno", "no", "lawno", "articletype"}:
            rendered = normalize_text(value)
            if rendered:
                parts.append(rendered)
    return "\n".join(parts)


def article_type(article: dict[str, Any]) -> str:
    return first_nonempty(article, "ArticleType", "Type", "條文類型")


def law_url(law: dict[str, Any], pcode: str = "") -> str:
    url = first_nonempty(law, "LawURL", "URL", "Url", "url")
    if url:
        return url
    code = pcode or first_nonempty(law, "PCode", "LawID", "LawNo")
    if code:
        return f"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode={code}"
    return ""


def law_full_text(law: dict[str, Any], name: str, articles: list[dict[str, Any]]) -> str:
    explicit = first_nonempty(law, "LawContent", "Content", "Text", "FullText", "全文")
    if explicit:
        return explicit
    lines = [name]
    foreword = first_nonempty(law, "Foreword", "Preamble", "序文")
    if foreword:
        lines.append(foreword)
    for article in articles:
        no = article_no(article)
        body = article_text(article)
        if no or body:
            lines.append("\n".join(x for x in [no, body] if x))
    return "\n\n".join(x for x in lines if x)


def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS laws (
          id INTEGER PRIMARY KEY,
          source TEXT NOT NULL,
          language TEXT NOT NULL,
          doc_type TEXT NOT NULL,
          name TEXT NOT NULL,
          level TEXT,
          category TEXT,
          modified_date TEXT,
          effective_date TEXT,
          update_date TEXT,
          url TEXT,
          histories TEXT,
          full_text TEXT,
          raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS articles (
          id INTEGER PRIMARY KEY,
          law_id INTEGER NOT NULL,
          source TEXT NOT NULL,
          language TEXT NOT NULL,
          doc_type TEXT NOT NULL,
          name TEXT NOT NULL,
          article_no TEXT,
          article_type TEXT,
          heading_path TEXT,
          text TEXT NOT NULL,
          url TEXT,
          modified_date TEXT,
          category TEXT,
          FOREIGN KEY(law_id) REFERENCES laws(id)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS laws_fts USING fts5(
          name, full_text, category, content='laws', content_rowid='id', tokenize='unicode61'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
          name, article_no, text, category, content='articles', content_rowid='id', tokenize='unicode61'
        );
        CREATE INDEX IF NOT EXISTS idx_laws_name ON laws(name);
        CREATE INDEX IF NOT EXISTS idx_articles_law_no ON articles(name, article_no);
        CREATE INDEX IF NOT EXISTS idx_articles_modified ON articles(modified_date);
        """
    )


def reset_source(con: sqlite3.Connection, source: str) -> None:
    con.execute("DELETE FROM articles_fts WHERE rowid IN (SELECT id FROM articles WHERE source=?)", (source,))
    con.execute("DELETE FROM laws_fts WHERE rowid IN (SELECT id FROM laws WHERE source=?)", (source,))
    con.execute("DELETE FROM articles WHERE source=?", (source,))
    con.execute("DELETE FROM laws WHERE source=?", (source,))


def import_json(db_path: Path, json_path: Path, source: str, endpoint: dict[str, str]) -> ImportStats:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    data = load_json(json_path)
    records = find_law_records(data)
    if not records:
        raise RuntimeError(f"cannot find law records in {json_path}")

    con = sqlite3.connect(db_path)
    create_schema(con)
    stats = ImportStats(source=source)
    with con:
        reset_source(con, source)
        for law in records:
            articles = article_list(law)
            name = first_nonempty(law, "LawName", "Name", "法規名稱")
            if not name:
                continue
            pcode = first_nonempty(law, "PCode", "LawID", "LawNo")
            url = law_url(law, pcode)
            full_text = law_full_text(law, name, articles)
            law_row = (
                source,
                endpoint["language"],
                endpoint["doc_type"],
                name,
                first_nonempty(law, "LawLevel", "Level", "位階"),
                first_nonempty(law, "LawCategory", "Category", "類別"),
                first_nonempty(law, "LawModifiedDate", "ModifiedDate", "modified_date", "修正日期"),
                first_nonempty(law, "LawEffectiveDate", "EffectiveDate", "effective_date", "施行日期"),
                first_nonempty(law, "UpdateDate", "update_date", "更新日期"),
                url,
                first_nonempty(law, "LawHistories", "Histories", "沿革"),
                full_text,
                json.dumps(law, ensure_ascii=False),
            )
            cur = con.execute(
                """
                INSERT INTO laws(source, language, doc_type, name, level, category, modified_date, effective_date,
                  update_date, url, histories, full_text, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                law_row,
            )
            law_id = cur.lastrowid
            con.execute("INSERT INTO laws_fts(rowid, name, full_text, category) VALUES (?, ?, ?, ?)", (law_id, name, full_text, law_row[5]))
            stats.laws += 1
            if not articles and full_text:
                articles = [{"ArticleNo": "", "ArticleContent": full_text, "ArticleType": "F"}]
            for article in articles:
                text = article_text(article)
                no = article_no(article)
                if not text and not no:
                    continue
                heading = pick(article, "HeadingPath", "heading_path", "章節")
                heading_text = json.dumps(heading, ensure_ascii=False) if isinstance(heading, (list, dict)) else normalize_text(heading)
                cur = con.execute(
                    """
                    INSERT INTO articles(law_id, source, language, doc_type, name, article_no, article_type,
                      heading_path, text, url, modified_date, category)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        law_id,
                        source,
                        endpoint["language"],
                        endpoint["doc_type"],
                        name,
                        no,
                        article_type(article),
                        heading_text,
                        text,
                        url,
                        law_row[6],
                        law_row[5],
                    ),
                )
                article_id = cur.lastrowid
                con.execute(
                    "INSERT INTO articles_fts(rowid, name, article_no, text, category) VALUES (?, ?, ?, ?, ?)",
                    (article_id, name, no, text, law_row[5]),
                )
                stats.articles += 1
        con.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (f"{source}.json", str(json_path)))
        con.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (f"{source}.imported_at", time.strftime("%Y-%m-%dT%H:%M:%S%z")))
    con.close()
    log("law_import_done", source=source, laws=stats.laws, articles=stats.articles)
    return stats


def fts_query(text: str) -> str:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", text)
    if not tokens:
        return '""'
    # FTS5 unicode61 does not segment Chinese, so keep exact quoted phrases and
    # meaningful substrings. LIKE fallback below catches partial Chinese terms.
    return " OR ".join(f'"{token}"' for token in tokens[:8])


def expand_aliases(query: str) -> str:
    expanded = query
    for alias, full in LAW_ALIASES.items():
        if alias in expanded and full not in expanded:
            expanded = expanded.replace(alias, f"{alias} {full}")
    return expanded


def normalized_article_marker(query: str) -> str:
    match = re.search(r"第\s*([0-9一二三四五六七八九十百千之\-]+)\s*條", query)
    if not match:
        return ""
    return f"第{match.group(1)}條"


def query_tokens(query: str) -> list[str]:
    expanded = expand_aliases(query)
    tokens = []
    for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", expanded):
        if token in {"法律", "法規", "法條", "條文", "規定", "查詢", "搜尋"}:
            continue
        tokens.append(token)
    article = normalized_article_marker(expanded)
    if article:
        tokens.append(article)
    deduped = []
    for token in tokens:
        if token not in deduped:
            deduped.append(token)
    return deduped[:12]


def law_name_hints(query: str) -> list[str]:
    expanded = expand_aliases(query)
    hints = []
    for alias, full in LAW_ALIASES.items():
        if alias in query or full in query:
            hints.append(full)
    for match in re.findall(r"[\u4e00-\u9fff]{1,20}法", expanded):
        if match not in hints:
            hints.append(match)
    if "民法" in expanded and "民法" not in hints:
        hints.append("民法")
    if "刑法" in expanded and "中華民國刑法" not in hints:
        hints.append("中華民國刑法")
    return hints[:5]


def rank_row(row: sqlite3.Row, query: str, tokens: list[str], law_hints: list[str], article: str) -> int:
    name = row["name"] or ""
    article_no = (row["article_no"] or "").replace(" ", "").replace("　", "")
    text = row["text"] or ""
    score = 0
    if article and article == article_no:
        score += 300
    elif article and article in article_no:
        score += 220
    for hint in law_hints:
        if hint == name:
            score += 180
        elif hint and hint in name:
            score += 120
    for token in tokens:
        if token in name:
            score += 35
        if token.replace(" ", "") in article_no:
            score += 60
        if token in text:
            score += 10
    if query in text:
        score += 80
    return score


def search(db_path: Path, query: str, limit: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    q = query.strip()
    expanded = expand_aliases(q)
    tokens = query_tokens(expanded)
    article = normalized_article_marker(expanded)
    law_hints = law_name_hints(expanded)
    fts = fts_query(q)
    rows = []
    if article and law_hints:
        article_like = f"%{article}%"
        law_clauses = " OR ".join(["name LIKE ?"] * len(law_hints))
        params = [f"%{hint}%" for hint in law_hints]
        params.extend([article_like, limit])
        rows = con.execute(
            f"""
            SELECT id, name, article_no, text, url, modified_date, category, doc_type, 0 AS score
            FROM articles
            WHERE ({law_clauses})
              AND replace(replace(article_no, ' ', ''), '　', '') LIKE ?
            LIMIT ?
            """,
            params,
        ).fetchall()
    try:
        if len(rows) < limit:
            fts_rows = con.execute(
                """
                SELECT a.id, a.name, a.article_no, a.text, a.url, a.modified_date, a.category, a.doc_type,
                       bm25(articles_fts) AS score
                FROM articles_fts
                JOIN articles a ON a.id = articles_fts.rowid
                WHERE articles_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (fts, max(limit, 20)),
            ).fetchall()
            seen = {r["id"] for r in rows}
            rows.extend([r for r in fts_rows if r["id"] not in seen])
    except sqlite3.OperationalError:
        pass

    if len(rows) < limit:
        like_tokens = tokens or [expanded]
        clauses = []
        params = []
        for token in like_tokens[:8]:
            like = f"%{token}%"
            clauses.append("(name LIKE ? OR article_no LIKE ? OR text LIKE ?)")
            params.extend([like, like, like])
        if article:
            clauses.append("replace(replace(article_no, ' ', ''), '　', '') LIKE ?")
            params.append(f"%{article}%")
        where = " OR ".join(clauses) if clauses else "name LIKE ? OR text LIKE ?"
        if not clauses:
            params.extend([f"%{expanded}%", f"%{expanded}%"])
        params.append(max(100, limit * 20))
        more = con.execute(
            f"""
            SELECT id, name, article_no, text, url, modified_date, category, doc_type, 0 AS score
            FROM articles
            WHERE {where}
            LIMIT ?
            """,
            params,
        ).fetchall()
        seen = {r["id"] for r in rows}
        rows.extend([r for r in more if r["id"] not in seen])

    rows = sorted(rows, key=lambda r: rank_row(r, expanded, tokens, law_hints, article), reverse=True)

    result_rows = []
    for row in rows[:limit]:
        text = row["text"] or ""
        result_rows.append({
            "name": row["name"],
            "article_no": row["article_no"],
            "text": text[:1200],
            "url": row["url"],
            "modified_date": row["modified_date"],
            "category": row["category"],
            "doc_type": row["doc_type"],
            "score": row["score"],
        })
    con.close()
    return {
        "ok": True,
        "query": query,
        "count": len(result_rows),
        "results": result_rows,
        "usedLlm": False,
        "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
        "source": "全國法規資料庫，法務部",
    }


def stats(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = {
        "db": str(db_path),
        "laws": con.execute("SELECT COUNT(*) FROM laws").fetchone()[0],
        "articles": con.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        "by_source": [dict(r) for r in con.execute("SELECT source, COUNT(*) laws FROM laws GROUP BY source ORDER BY source")],
        "article_by_source": [dict(r) for r in con.execute("SELECT source, COUNT(*) articles FROM articles GROUP BY source ORDER BY source")],
        "metadata": {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM metadata ORDER BY key")},
    }
    con.close()
    return out


def cmd_mirror(args: argparse.Namespace) -> int:
    raw_dir = args.data_dir / "raw"
    sources = args.sources or list(ENDPOINTS)
    for source in sources:
        if source not in ENDPOINTS:
            raise SystemExit(f"unknown source: {source}")
        path = download_endpoint(source, ENDPOINTS[source], raw_dir, force=args.force)
        import_json(args.db, path, source, ENDPOINTS[source])
    print(json.dumps(stats(args.db), ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    print(json.dumps(search(args.db, args.query, args.limit), ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    print(json.dumps(stats(args.db), ensure_ascii=False, indent=2))
    return 0


class LawLookupHandler(BaseHTTPRequestHandler):
    server_version = "AliceTaiwanLawLookup/1.0"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "service": "alice-tw-law-local", "usedLlm": False, "db": str(self.server.db_path)})
            return
        if parsed.path == "/stats":
            self._send(200, stats(self.server.db_path))
            return
        if parsed.path != "/search":
            self._send(404, {"ok": False, "error": "not found"})
            return
        query = parse_qs(parsed.query)
        self._send(200, search(self.server.db_path, query.get("q", [""])[0], int(query.get("limit", ["8"])[0])))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/search":
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("content-length", "0"))
        if length > 8192:
            self._send(413, {"ok": False, "error": "payload too large"})
            return
        body = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            payload = {"q": body}
        self._send(200, search(self.server.db_path, str(payload.get("q") or payload.get("query") or ""), int(payload.get("limit") or 8)))

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(json.dumps({"event": "law_http", "message": fmt % args}, ensure_ascii=False) + "\n")


def cmd_serve(args: argparse.Namespace) -> int:
    server = ThreadingHTTPServer((args.host, args.port), LawLookupHandler)
    server.db_path = args.db
    print(json.dumps({"event": "started", "service": "alice-tw-law-local", "host": args.host, "port": args.port, "db": str(args.db)}, ensure_ascii=False), flush=True)
    server.serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alice local Taiwan law mirror/search")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd", required=True)

    mirror = sub.add_parser("mirror", help="Download official MOJ ZIP JSON and rebuild local SQLite FTS")
    mirror.add_argument("--force", action="store_true")
    mirror.add_argument("--sources", nargs="*", choices=list(ENDPOINTS))
    mirror.set_defaults(func=cmd_mirror)

    search_cmd = sub.add_parser("search", help="Search local law SQLite FTS")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=8)
    search_cmd.set_defaults(func=cmd_search)

    stats_cmd = sub.add_parser("stats")
    stats_cmd.set_defaults(func=cmd_stats)

    serve_cmd = sub.add_parser("serve", help="Run local HTTP law lookup service")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8013)
    serve_cmd.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
