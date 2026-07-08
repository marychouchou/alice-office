#!/usr/bin/env python3
"""Query Alice's offline China/Chinese AI assistant ecosystem index."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_INDEX = Path(__file__).with_name("china-ai-assistant-index.json")


def load_index(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"index not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid index json: {path}: {exc}") from None


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def project_text(project: dict[str, Any]) -> str:
    fields: list[str] = [
        str(project.get("id", "")),
        str(project.get("name", "")),
        str(project.get("category", "")),
        str(project.get("why_it_matters_to_alice", "")),
        str(project.get("alice_integration", {}).get("use", "")),
    ]
    fields.extend(str(x) for x in project.get("aliases", []) or [])
    fields.extend(str(x) for x in project.get("capabilities", []) or [])
    fields.extend(str(x) for x in project.get("urls", []) or [])
    fields.extend(str(x) for x in project.get("alice_integration", {}).get("candidate_modules", []) or [])
    return normalize(" ".join(fields))


def tokenize(query: str) -> list[str]:
    cleaned = normalize(query)
    raw_tokens = re.split(r"[\s,，。:：/|]+", cleaned)
    tokens = [token for token in raw_tokens if token]
    chinese_keywords = [
        "記憶",
        "记忆",
        "長期",
        "长期",
        "秘書",
        "秘书",
        "助理",
        "助手",
        "管家",
        "幫傭",
        "帮佣",
        "家居",
        "辦公",
        "办公",
        "微信",
        "企業微信",
        "企业微信",
        "飛書",
        "飞书",
        "釘釘",
        "钉钉",
        "語音",
        "语音",
        "排程",
        "定時",
        "定时",
        "工作流",
        "瀏覽器",
        "浏览器",
        "開源",
        "开源",
    ]
    tokens.extend(keyword for keyword in chinese_keywords if keyword in cleaned)
    return list(dict.fromkeys(tokens))


def score_project(project: dict[str, Any], query: str) -> int:
    text = project_text(project)
    score = 0
    for token in tokenize(query):
        if token in text:
            score += 4 if len(token) >= 3 else 2
    priority = project.get("alice_integration", {}).get("priority", "")
    if priority == "P0":
        score += 3
    elif priority == "P1":
        score += 1
    return score


def search_projects(index: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for project in index.get("projects", []):
        score = score_project(project, query)
        if score > 0:
            scored.append((score, project))
    scored.sort(key=lambda item: (-item[0], item[1].get("id", "")))
    return [project | {"score": score} for score, project in scored[:limit]]


def list_projects(index: dict[str, Any], category: str | None = None) -> list[dict[str, Any]]:
    projects = index.get("projects", [])
    if category:
        category_norm = normalize(category)
        projects = [project for project in projects if category_norm in normalize(str(project.get("category", "")))]
    return sorted(projects, key=lambda project: (project.get("alice_integration", {}).get("priority", "P9"), project.get("id", "")))


def recommend(index: dict[str, Any], need: str, limit: int) -> list[dict[str, Any]]:
    synonyms = {
        "memory": "long_term_memory 記憶 长期记忆 越用越懂 skills",
        "office": "office_tools 辦公 秘書 企业微信 calendar documents todo wecom",
        "home": "home_automation smart_home_housekeeper 家居 管家 iot Home Assistant 小智",
        "voice": "voice 語音 小智 esp32 asr tts wake_word",
        "workflow": "workflow event_driven interrupt resume job_queue 工作流 中斷 恢復",
        "channels": "wechat feishu dingtalk qq line gateway 多通道",
        "skills": "skills skill_generation skill_market deterministic tool router",
    }
    query = synonyms.get(normalize(need), need)
    return search_projects(index, query, limit)


def render_markdown(projects: list[dict[str, Any]]) -> str:
    if not projects:
        return "沒有找到符合條件的專案。"
    lines = ["| 專案 | 優先 | 類型 | Alice 可借的點 |", "|---|---|---|---|"]
    for project in projects:
        priority = project.get("alice_integration", {}).get("priority", "")
        use = project.get("alice_integration", {}).get("use", "")
        url = (project.get("urls") or [""])[0]
        name = project.get("name", project.get("id", ""))
        label = f"[{name}]({url})" if url else str(name)
        lines.append(f"| {label} | {priority} | {project.get('category', '')} | {use} |")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default=str(DEFAULT_INDEX), help="Path to china-ai-assistant-index.json")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="List indexed projects")
    list_cmd.add_argument("--category", default="", help="Filter by category substring")

    search_cmd = sub.add_parser("search", help="Search projects by query")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=8)

    recommend_cmd = sub.add_parser("recommend", help="Recommend projects for an Alice capability")
    recommend_cmd.add_argument("need", help="memory, office, home, voice, workflow, channels, skills, or free text")
    recommend_cmd.add_argument("--limit", type=int, default=6)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    index = load_index(Path(args.index).expanduser())

    if args.command == "list":
        projects = list_projects(index, args.category or None)
    elif args.command == "search":
        projects = search_projects(index, args.query, args.limit)
    elif args.command == "recommend":
        projects = recommend(index, args.need, args.limit)
    else:
        parser.error("unknown command")

    if args.json:
        print(json.dumps({"ok": True, "projects": projects}, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(projects))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
