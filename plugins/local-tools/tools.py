"""Tool handlers for the local-tools plugin.

Each handler runs its corresponding alice-tools-pack script via subprocess
(/usr/bin/python3), parses the JSON stdout, and returns a JSON string.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Any
import yaml

TOOLS_ROOT = Path(__file__).parent / "scripts"
# Use the same interpreter that runs the Hermes agent process — the plugin
# scripts share Hermes's Python (and its installed packages like sympy/fitz).
PYTHON = sys.executable

# Plugin data lives under HERMES_HOME/local-tools-data/ so the directory is
# clearly associated with this plugin and not with any previous agent setup.
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
_PLUGIN_DATA = _HERMES_HOME / "local-tools-data"

# Inherit current env, then override hardcoded OpenClaw paths with
# plugin-owned directories under HERMES_HOME/local-tools-data/.
_BASE_ENV: dict[str, str] = {
    **os.environ,
    "ALICE_TW_LAW_DATA_DIR": str(_PLUGIN_DATA / "law-data"),
    "ALICE_TW_LAW_DB":       str(_PLUGIN_DATA / "law-data" / "tw-law.sqlite"),
    "ALICE_MEMORY_DB":       str(_PLUGIN_DATA / "memory" / "alice-memory.sqlite"),
}

_BROWSER_ENV: dict[str, str] = {
    **_BASE_ENV,
    "ALICE_BROWSER_HOME":        str(_PLUGIN_DATA / "browser"),
    "ALICE_BROWSER_LOCK":        str(_PLUGIN_DATA / "browser" / "browser.lock"),
    "ALICE_BROWSER_PROFILE":     str(_PLUGIN_DATA / "browser" / "firefox-profile"),
    "ALICE_BROWSER_SCREENSHOTS": str(_PLUGIN_DATA / "browser" / "screenshots"),
    "ALICE_ECOMMERCE_INDEX":     str(TOOLS_ROOT / "browser" / "tw-ecommerce-index.json"),
    "ALICE_GECKODRIVER_BIN":     "/usr/local/bin/geckodriver",
}


def _run(script: Path, argv: list[str], timeout: int = 60,
         env: dict[str, str] | None = None) -> str:
    """Run a tool script and return its JSON stdout as a JSON string."""
    try:
        proc = subprocess.run(
            [PYTHON, str(script), *argv],
            capture_output=True, text=True,
            env=env if env is not None else _BASE_ENV,
            timeout=timeout,
        )
        stdout = proc.stdout.strip()
        if stdout:
            try:
                return json.dumps(json.loads(stdout), ensure_ascii=False)
            except json.JSONDecodeError:
                return json.dumps({"output": stdout}, ensure_ascii=False)
        err = proc.stderr.strip()
        return json.dumps({"error": err or f"exit {proc.returncode}"}, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "tool timed out"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


# ─── payroll_generate ────────────────────────────────────────────────────────

_PAYROLL_SCRIPT = TOOLS_ROOT / "hr" / "alice-payroll-engine.py"
_PAYROLL_CONFIG = TOOLS_ROOT / "hr" / "alice-payroll-config.2026.tw.json"


def handle_hr(args: dict, **_: Any) -> str:
    employees_csv = args.get("employees_csv", "")
    attendance_csv = args.get("attendance_csv", "")
    out_xlsx = args.get("out_xlsx", "")
    config_path = args.get("config_path", "")
    argv = [
        "generate",
        "--employees", employees_csv,
        "--attendance", attendance_csv,
        "--out", out_xlsx,
        "--config", config_path or str(_PAYROLL_CONFIG),
    ]
    return _run(_PAYROLL_SCRIPT, argv)


# ─── tw_law ──────────────────────────────────────────────────────────────────

_LAW_SCRIPT = TOOLS_ROOT / "law" / "alice-tw-law-local.py"


def handle_law(args: dict, **_: Any) -> str:
    command = args.get("command", "")
    query = args.get("query", "")
    limit = int(args.get("limit", 8))
    force = bool(args.get("force", False))
    sources = args.get("sources")
    if command == "search":
        if not query:
            return json.dumps({"error": "command=search 需要 query 參數"}, ensure_ascii=False)
        argv = ["search", query, "--limit", str(limit)]
    elif command == "stats":
        argv = ["stats"]
    elif command == "mirror":
        argv = ["mirror"]
        if force:
            argv.append("--force")
        if sources:
            argv += ["--sources"] + list(sources)
    else:
        return json.dumps({"error": f"未知的 command: {command!r}"}, ensure_ascii=False)
    return _run(_LAW_SCRIPT, argv, timeout=300)


# ─── engineering_calc ────────────────────────────────────────────────────────

_MATH_SCRIPT = TOOLS_ROOT / "math" / "alice-engineering-calculator.py"


def handle_math(args: dict, **_: Any) -> str:
    expression = args.get("expression", "")
    return _run(_MATH_SCRIPT, [expression])


# ─── long_term_memory ────────────────────────────────────────────────────────

_MEMORY_SCRIPT = TOOLS_ROOT / "memory" / "alice-long-term-memory.py"


def handle_longmem(args: dict, **_: Any) -> str:
    command = args.get("command", "")
    user_id = args.get("user_id", "")
    text = args.get("text", "")
    query = args.get("query", "")
    role = args.get("role", "user")
    conversation_id = args.get("conversation_id", "")
    memory_type = args.get("memory_type", "preference")
    title = args.get("title", "")
    memory_id = args.get("memory_id", "")
    limit = int(args.get("limit", 8))
    if command == "remember":
        if not text:
            return json.dumps({"error": "command=remember 需要 text 參數"}, ensure_ascii=False)
        argv = ["remember", "--user-id", user_id, "--text", text, "--type", memory_type]
        if title:
            argv += ["--title", title]
    elif command == "recall":
        argv = ["recall", "--user-id", user_id, "--limit", str(limit)]
        if query:
            argv += ["--query", query]
    elif command == "context":
        argv = ["context", "--user-id", user_id, "--limit", str(limit)]
        if query:
            argv += ["--query", query]
    elif command == "record_turn":
        if not text:
            return json.dumps({"error": "command=record_turn 需要 text 參數"}, ensure_ascii=False)
        argv = ["record-turn", "--user-id", user_id, "--role", role, "--text", text]
        if conversation_id:
            argv += ["--conversation-id", conversation_id]
    elif command == "delete":
        if not memory_id:
            return json.dumps({"error": "command=delete 需要 memory_id 參數"}, ensure_ascii=False)
        argv = ["delete", "--user-id", user_id, "--memory-id", memory_id]
    else:
        return json.dumps({"error": f"未知的 command: {command!r}"}, ensure_ascii=False)
    return _run(_MEMORY_SCRIPT, argv)


# ─── assistant_ecosystem ─────────────────────────────────────────────────────

_RESEARCH_SCRIPT = TOOLS_ROOT / "research" / "alice-assistant-ecosystem.py"
_RESEARCH_INDEX = TOOLS_ROOT / "research" / "china-ai-assistant-index.json"


def handle_research(args: dict, **_: Any) -> str:
    command = args.get("command", "")
    query = args.get("query", "")
    need = args.get("need", "")
    category = args.get("category", "")
    limit = int(args.get("limit", 8))
    base = ["--index", str(_RESEARCH_INDEX), "--json"]
    if command == "search":
        if not query:
            return json.dumps({"error": "command=search 需要 query 參數"}, ensure_ascii=False)
        argv = base + ["search", query, "--limit", str(limit)]
    elif command == "list":
        argv = base + ["list"]
        if category:
            argv += ["--category", category]
    elif command == "recommend":
        if not need:
            return json.dumps({"error": "command=recommend 需要 need 參數"}, ensure_ascii=False)
        argv = base + ["recommend", need, "--limit", str(limit)]
    else:
        return json.dumps({"error": f"未知的 command: {command!r}"}, ensure_ascii=False)
    return _run(_RESEARCH_SCRIPT, argv)


# ─── image_ocr ───────────────────────────────────────────────────────────

def _load_hermes_dotenv() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in (_HERMES_HOME / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    except Exception:
        pass
    return result


def _load_vision_config() -> tuple[str, str]:
    try:
        cfg = yaml.safe_load((_HERMES_HOME / "config.yaml").read_text(encoding="utf-8"))
        vision = cfg.get("auxiliary", {}).get("vision", {})
        base_url = str(vision.get("base_url", "")).rstrip("/")
        model = str(vision.get("model", "qwen2.5-vl"))
        if base_url:
            return f"{base_url}/chat/completions", model
    except Exception:
        pass
    return "http://127.0.0.1:8001/v1/chat/completions", "qwen2.5-vl"


_HERMES_DOTENV = _load_hermes_dotenv()
_VISION_URL, _VISION_MODEL = _load_vision_config()

_OCR_SCRIPT = TOOLS_ROOT / "image-ocr" / "alice-image-exam-ocr.py"
_OCR_ENV: dict[str, str] = {
    **_BASE_ENV,
    "ALICE_IMAGE_OCR_CACHE_DIR": str(_PLUGIN_DATA / "image-ocr-cache"),
    "ALICE_VISION_CHAT_URL":     _VISION_URL,
    "ALICE_VISION_MODEL":        _VISION_MODEL,
    "OPENAI_API_KEY":            _HERMES_DOTENV.get("OPENAI_API_KEY", _BASE_ENV.get("OPENAI_API_KEY", "")),
}


def handle_ocr(args: dict, **_: Any) -> str:
    path = args.get("path", "")
    prompt = args.get("prompt", "")
    if not path:
        return json.dumps({"error": "path 參數必填"}, ensure_ascii=False)
    argv = ["--path", path]
    if prompt:
        argv += ["--prompt", prompt]
    return _run(_OCR_SCRIPT, argv, timeout=120, env=_OCR_ENV)


# ─── pre_llm_call hook — 自動 OCR 傳入的 PDF ────────────────────────────────

_DOC_PATH_RE = re.compile(r"It is saved at:\s*([^\n]+?\.pdf)\b", re.IGNORECASE)


def pre_llm_call_ocr_hook(*, user_message: str = "", **_: Any) -> dict | None:
    """在 LLM 收到訊息前，自動辨識訊息中的 PDF 並將 OCR 結果注入 context。"""
    match = _DOC_PATH_RE.search(user_message or "")
    if not match:
        return None
    pdf_path = match.group(1).strip().rstrip(".")
    if not Path(pdf_path).exists():
        return None
    result = handle_ocr({"path": pdf_path})
    try:
        data = json.loads(result)
        if data.get("ok") and data.get("text"):
            return {"context": f"[PDF OCR 辨識結果：{Path(pdf_path).name}]\n{data['text']}"}
    except Exception:
        pass
    return None


# ─── browser_task ────────────────────────────────────────────────────────────

_BROWSER_SCRIPT = TOOLS_ROOT / "browser" / "alice-browser-task.py"


def check_browser_available() -> bool:
    return which("geckodriver") is not None and _BROWSER_SCRIPT.exists()


def handle_webdriver(args: dict, **_: Any) -> str:
    command = args.get("command", "")
    url = args.get("url", "")
    instruction = args.get("instruction", "")
    pickup = args.get("pickup", "")
    dropoff = args.get("dropoff", "")
    days = int(args.get("days", 7))
    if command == "cleanup":
        argv: list[str] = ["cleanup", "--days", str(days)]
    elif command == "health":
        argv = ["health"]
    elif command == "open":
        if not url:
            return json.dumps({"error": "command=open 需要 url 參數"}, ensure_ascii=False)
        argv = ["open", "--url", url]
    elif command == "shopping":
        argv = ["shopping"]
        if instruction:
            argv += ["--instruction", instruction]
        if url:
            argv += ["--url", url]
    elif command == "uber":
        argv = ["uber"]
        if instruction:
            argv += ["--instruction", instruction]
        if pickup:
            argv += ["--pickup", pickup]
        if dropoff:
            argv += ["--dropoff", dropoff]
    else:
        return json.dumps({"error": f"未知的 command: {command!r}"}, ensure_ascii=False)
    return _run(_BROWSER_SCRIPT, argv, timeout=90, env=_BROWSER_ENV)
