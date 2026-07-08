#!/usr/bin/env python3
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "law" / "alice-tw-law-local.py"


def test_search_fixture():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "law.sqlite"
        con = sqlite3.connect(db)
        import importlib.util
        spec = importlib.util.spec_from_file_location("lawtool", TOOL)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["lawtool"] = mod
        spec.loader.exec_module(mod)
        mod.create_schema(con)
        with con:
            cur = con.execute(
                "INSERT INTO laws(source, language, doc_type, name, full_text) VALUES (?, ?, ?, ?, ?)",
                ("fixture", "ch", "law", "民法", "民法 第 184 條 因故意或過失，不法侵害他人之權利者，負損害賠償責任。"),
            )
            law_id = cur.lastrowid
            con.execute("INSERT INTO laws_fts(rowid, name, full_text, category) VALUES (?, ?, ?, ?)", (law_id, "民法", "民法 第 184 條 因故意或過失，不法侵害他人之權利者，負損害賠償責任。", "民事"))
            cur = con.execute(
                "INSERT INTO articles(law_id, source, language, doc_type, name, article_no, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (law_id, "fixture", "ch", "law", "民法", "第 184 條", "因故意或過失，不法侵害他人之權利者，負損害賠償責任。"),
            )
            article_id = cur.lastrowid
            con.execute("INSERT INTO articles_fts(rowid, name, article_no, text, category) VALUES (?, ?, ?, ?, ?)", (article_id, "民法", "第 184 條", "因故意或過失，不法侵害他人之權利者，負損害賠償責任。", "民事"))
        con.close()
        out = subprocess.check_output([sys.executable, str(TOOL), "--db", str(db), "search", "民法 184 損害賠償"], text=True)
        data = json.loads(out)
        assert data["ok"] is True, data
        assert data["usedLlm"] is False, data
        assert data["count"] >= 1, data
        assert data["results"][0]["name"] == "民法", data


if __name__ == "__main__":
    test_search_fixture()
    print("TW_LAW_LOCAL_TESTS_OK")
