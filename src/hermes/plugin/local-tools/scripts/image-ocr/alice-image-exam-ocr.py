#!/usr/bin/env python3
"""圖像 OCR 工具 — 呼叫本機 Qwen2.5-VL 辨識圖片或 PDF 文字。

用法：
  python3 alice-image-exam-ocr.py --path /path/to/image.jpg [--prompt "..."]

回傳 JSON：{ ok, text, image_hash, cache_hit, mime, elapsed_ms }
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

VISION_URL = os.environ.get("ALICE_VISION_CHAT_URL", "http://127.0.0.1:8001/v1/chat/completions")
VISION_MODEL = os.environ.get("ALICE_VISION_MODEL", "qwen2.5-vl")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
CACHE_DIR = Path(
    os.environ.get(
        "ALICE_IMAGE_OCR_CACHE_DIR",
        Path.home() / ".hermes/local-tools-data/image-ocr-cache",
    )
)

DEFAULT_PROMPT = (
    "請辨識並完整抄錄圖片中所有可見文字，保留原始段落與題號格式。"
    "若有題目與選項請依序列出。若非文件型圖片，請描述主要內容、物件與場景。"
    "不要編造不可見的細節。"
)


def _sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _read_cache(key: str) -> dict | None:
    try:
        return json.loads(_cache_path(key).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(key: str, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _image_to_data_url(path: str) -> tuple[str, str]:
    """Returns (data_url, mime). PDF is rendered at 2x scale to first-page PNG."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pdf":
        import fitz  # pymupdf

        doc = fitz.open(str(p))
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        img_bytes = pix.tobytes("png")
        doc.close()
        mime = "image/png"
    else:
        img_bytes = p.read_bytes()
        mime = {"png": "image/png", "webp": "image/webp"}.get(
            ext.lstrip("."), "image/jpeg"
        )
    b64 = base64.b64encode(img_bytes).decode()
    return f"data:{mime};base64,{b64}", mime


def _call_vision(data_url: str, prompt: str) -> str:
    body = json.dumps(
        {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 Alice 的圖像辨識助手。請用繁體中文回答。"
                        "不要編造圖片中不可見的細節；不確定請說「可能」。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 1500,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        ensure_ascii=False,
    ).encode()

    req = urllib.request.Request(
        VISION_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "OpenAI/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read())
    return str(payload["choices"][0]["message"]["content"]).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, help="圖片或 PDF 路徑")
    parser.add_argument("--prompt", default="", help="辨識提示（選填）")
    parsed = parser.parse_args()

    started = time.time()
    path = parsed.path
    prompt = parsed.prompt.strip() or DEFAULT_PROMPT

    if not Path(path).exists():
        print(json.dumps({"ok": False, "error": f"檔案不存在: {path}"}, ensure_ascii=False))
        return

    image_hash = _sha256_file(path)
    cache_key = f"{image_hash}-{_short_hash(prompt)}"

    cached = _read_cache(cache_key)
    if cached and cached.get("ok"):
        print(
            json.dumps(
                {**cached, "cache_hit": True, "elapsed_ms": int((time.time() - started) * 1000)},
                ensure_ascii=False,
            )
        )
        return

    try:
        data_url, mime = _image_to_data_url(path)
        text = _call_vision(data_url, prompt)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return

    result = {
        "ok": True,
        "text": text,
        "image_hash": image_hash,
        "cache_key": cache_key,
        "cache_hit": False,
        "mime": mime,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    _write_cache(cache_key, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
