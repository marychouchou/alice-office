"""Tool handlers for the local-tools plugin.

Each handler runs its corresponding alice-tools-pack script via subprocess,
parses the JSON stdout, and returns a JSON string. Scripts run under the
shared tools venv at /opt/tools/.venv (see src/hermes/runtime/), which is
baked into the Hermes image by Dockerfile.hermes and exposed to this
process via the TOOLS_PYTHON env var — NOT Hermes's own venv/interpreter
(sys.executable), which only carries pyyaml for the in-process plugin
layer itself.

Reading files a user sent in goes through image_ocr alone:
_load_vision_config reads the room's config.yaml so image_ocr talks to the
room's own main model on its own provider/key — the main model is multimodal —
instead of a local vision server that doesn't exist inside the container.

Runs in-process inside Hermes, so it must stay stdlib + pyyaml only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Any
import yaml

TOOLS_ROOT = Path(__file__).parent / "scripts"
# Prefer the shared tools venv (sympy/fitz/selenium live there — see
# src/hermes/runtime/pyproject.toml); fall back to the current interpreter
# for host-side Level-0 testing or older images that predate TOOLS_PYTHON.
_TOOLS_PYTHON = os.environ.get("TOOLS_PYTHON", "/opt/tools/.venv/bin/python3")
PYTHON = _TOOLS_PYTHON if Path(_TOOLS_PYTHON).is_file() else sys.executable

def _hermes_home() -> Path:
    """Resolve HERMES_HOME now, not at import time (= /opt/data in a room)."""
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


# Plugin data lives under HERMES_HOME/local-tools-data/ so the directory is
# clearly associated with this plugin and not with any previous agent setup.
# The module-level snapshot is what the subprocess env below is built from
# (it never changes during a container's life); handle_share_file calls
# _hermes_home() per invocation instead, so a test can point it elsewhere.
_HERMES_HOME = _hermes_home()
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

_DEFAULT_VISION_URL = "http://127.0.0.1:8001/v1/chat/completions"
_DEFAULT_VISION_MODEL = "qwen2.5-vl"
_DEFAULT_KEY_ENV = "LLM_API_KEY"


def _load_hermes_dotenv(hermes_home: Path) -> dict[str, str]:
    """Parse HERMES_HOME/.env into a dict (missing/unreadable file -> {})."""
    result: dict[str, str] = {}
    try:
        for line in (hermes_home / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    except Exception:
        pass
    return result


def _load_vision_config(hermes_home: Path) -> tuple[str, str, str]:
    """Resolve (chat_completions_url, model, api_key) for image recognition.

    Reads the room's config.yaml, which the router renders from
    src/hermes/config.template.yml:

        model:
          default: qwen3.6-35b   # the main model — already multimodal
          provider: custom
        providers:
          custom:
            base_url: https://.../v1
            key_env: LLM_API_KEY

    Default = the room's MAIN model on its own provider: same endpoint, same
    key, no extra configuration. There is no separate vision server inside the
    container, which is why the old `auxiliary.vision.base_url` fallback of
    127.0.0.1:8001 made every call die with "Connection refused"; likewise the
    key came from a `HERMES_HOME/.env` that doesn't exist, instead of the
    container's real `LLM_API_KEY`.

    A room that really does run a different vision model/endpoint can still
    override it by hand in its own config.yaml:

        auxiliary:
          vision:
            model: qwen2.5-vl
            base_url: http://.../v1   # optional

    Args:
        hermes_home: The Hermes home directory holding config.yaml and .env
            (/opt/data inside a room's container).

    Returns:
        (url, model, api_key). url always ends in /chat/completions; api_key
        may be "" when nothing is configured (the call then fails loudly at
        the endpoint rather than silently here).
    """
    url, model, key_env = _DEFAULT_VISION_URL, _DEFAULT_VISION_MODEL, _DEFAULT_KEY_ENV
    try:
        cfg = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    except Exception:
        cfg = None
    if isinstance(cfg, dict):
        vision = (cfg.get("auxiliary") or {}).get("vision") or {}
        main_model = cfg.get("model") or {}
        provider_name = str(main_model.get("provider") or "custom")
        provider = (cfg.get("providers") or {}).get(provider_name) or {}
        model = str(vision.get("model") or main_model.get("default") or model)
        key_env = str(provider.get("key_env") or key_env)
        base_url = str(vision.get("base_url") or provider.get("base_url") or "").rstrip("/")
        if base_url:
            url = f"{base_url}/chat/completions"
    dotenv = _load_hermes_dotenv(hermes_home)
    api_key = (
        os.environ.get(key_env, "")
        or dotenv.get("OPENAI_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    return url, model, api_key


_VISION_URL, _VISION_MODEL, _VISION_API_KEY = _load_vision_config(_HERMES_HOME)

_OCR_SCRIPT = TOOLS_ROOT / "image-ocr" / "alice-image-exam-ocr.py"
_OCR_ENV: dict[str, str] = {
    **_BASE_ENV,
    "ALICE_IMAGE_OCR_CACHE_DIR": str(_PLUGIN_DATA / "image-ocr-cache"),
    "ALICE_VISION_CHAT_URL":     _VISION_URL,
    "ALICE_VISION_MODEL":        _VISION_MODEL,
    "ALICE_VISION_API_KEY":      _VISION_API_KEY,
    # Kept for compatibility with older copies of the script (and any other
    # OpenAI-flavoured tool) that only look at OPENAI_API_KEY.
    "OPENAI_API_KEY":            _VISION_API_KEY or _BASE_ENV.get("OPENAI_API_KEY", ""),
}


# 刻意「沒有」pre_llm_call hook 自動注入檔案內容（2026-09-15 移除）：Hermes 的
# hook context 只加進「這一輪送出去的訊息副本」，原始訊息不會被改寫，所以注入的內容
# 從來沒進過 session 持久化——下一輪模型就看不到它了，卻仍以為自己讀過這份檔案，
# 於是開始憑印象編造內容（實測：turn 2 少了約 6,100 input tokens，答案整段虛構）。
# 工具結果則相反，會留在 session 逐字稿裡，而模型看到 router 的檔案提示就會自己呼叫
# image_ocr（有快取，重複呼叫幾乎不花時間）。所以這裡只留工具，不留 hook。
def handle_ocr(args: dict, **_: Any) -> str:
    path = args.get("path", "")
    prompt = args.get("prompt", "")
    if not path:
        return json.dumps({"error": "path 參數必填"}, ensure_ascii=False)
    argv = ["--path", path]
    if prompt:
        argv += ["--prompt", prompt]
    return _run(_OCR_SCRIPT, argv, timeout=120, env=_OCR_ENV)


# ─── share_file ──────────────────────────────────────────────────────────────

# LINE lets a bot send no file at all, so the only way to hand the user
# something the agent produced is a download link from the router. This tool
# is the container's half: copy the file into HERMES_HOME/outbox/<token>/ and
# return the placeholder outbox://<token>, which the router swaps for a real
# URL on its way out (see src/alice_office_router/file_links.py and
# docs/file-share-design.md). The container never learns the room id or the
# router's public URL, so no new container env var is needed.
#
# Deliberately NOT restricted to files under HERMES_HOME: the hr tool writes
# its xlsx to /tmp by design, and inside the container /tmp and /opt/data are
# not a privilege boundary anyway. The real fence is on the router side, which
# re-validates and copies the file out of this room's mount before serving it.
_SHARE_MAX_BYTES = 50 * 1024 * 1024

# Mirrors file_links._UNSAFE_NAME_RE on the router side (control characters,
# quotes, backslash, separator) — the router sanitizes again regardless, since
# the agent can write to outbox/ without going through this tool.
_SHARE_UNSAFE_NAME_RE = re.compile(r'[\x00-\x1f\x7f"\\/]')


def handle_share_file(args: dict, **_: Any) -> str:
    path = str(args.get("path", "")).strip()
    if not path:
        return json.dumps({"error": "path 參數必填"}, ensure_ascii=False)
    source = Path(path).expanduser()
    if not source.is_file():
        return json.dumps(
            {"error": f"找不到檔案（或不是一般檔案）：{path}"}, ensure_ascii=False
        )
    size = source.stat().st_size
    if size > _SHARE_MAX_BYTES:
        return json.dumps(
            {"error": f"檔案 {size} bytes 超過上限 {_SHARE_MAX_BYTES} bytes，無法分享"},
            ensure_ascii=False,
        )
    name = _SHARE_UNSAFE_NAME_RE.sub("_", source.name).strip().lstrip(".") or "file"
    token = secrets.token_urlsafe(32)
    dest_dir = _hermes_home() / "outbox" / token
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest_dir / name)
    except OSError as exc:
        return json.dumps(
            {"error": f"複製到 outbox 失敗：{type(exc).__name__}: {exc}"}, ensure_ascii=False
        )
    return json.dumps(
        {
            "link": f"outbox://{token}",
            "filename": name,
            "bytes": size,
            "instruction": "把 link 原樣、單獨一行貼進回覆；不要改寫它，也不要包成 markdown 連結。",
        },
        ensure_ascii=False,
    )


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
