#!/usr/bin/env python3
"""文件讀取工具 — 抽出 PDF 文字層，掃描頁才呼叫 vision 模型辨識。

PDF：逐頁處理（最多 --max-pages 頁，預設 30）。有文字層的頁直接用 pymupdf
抽文字，完全不碰網路；文字太少（掃描／拍照頁）才把該頁算圖送 vision 模型。
所以整份都有文字層的 PDF 不需要 vision endpoint 也能讀完。
圖片（jpg/png/webp）：照舊，單次 vision 呼叫。

vision endpoint／model／金鑰由 plugin 的 tools.py 從房間 config.yaml 推導後，
以 ALICE_VISION_CHAT_URL / ALICE_VISION_MODEL / ALICE_VISION_API_KEY 傳進來。

用法：
  python3 alice-image-exam-ocr.py --path /path/to/file.pdf [--prompt "..."] [--max-pages 30]

回傳 JSON：
  { ok, text, pages, ocr_pages, image_hash, cache_hit, mime, elapsed_ms }
  pages     = 實際處理的頁數（圖片為 1）
  ocr_pages = 真的走了 vision 辨識的頁碼清單（全文字層的 PDF 是 []）
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
API_KEY = os.environ.get("ALICE_VISION_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
CACHE_DIR = Path(
    os.environ.get(
        "ALICE_IMAGE_OCR_CACHE_DIR",
        Path.home() / ".hermes/local-tools-data/image-ocr-cache",
    )
)

# 一頁抽出的文字少於這個字數就當成掃描／拍照頁，改走 vision 辨識。
TEXT_LAYER_MIN_CHARS = 20
# PDF 預設最多處理幾頁（--max-pages 可調）：擋住幾百頁的檔案把 context 與
# vision 呼叫次數撐爆。
DEFAULT_MAX_PAGES = 30

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


def _to_data_url(img_bytes: bytes, mime: str) -> str:
    """Wrap raw image bytes as a data: URL for the vision API."""
    return f"data:{mime};base64,{base64.b64encode(img_bytes).decode()}"


def _image_data_url(path: str) -> tuple[str, str]:
    """Read a (non-PDF) image file. Returns (data_url, mime)."""
    p = Path(path)
    mime = {"png": "image/png", "webp": "image/webp"}.get(
        p.suffix.lower().lstrip("."), "image/jpeg"
    )
    return _to_data_url(p.read_bytes(), mime), mime


def _join_pages(pages: list[str]) -> str:
    """把逐頁文字合併成一份文件文字，每頁前面加頁碼標題。

    純函式（不碰 pymupdf、不連網），方便單獨測試。
    """
    return "\n\n".join(f"--- 第 {n} 頁 ---\n{text}" for n, text in enumerate(pages, 1))


def _extract_pdf(path: str, prompt: str, max_pages: int) -> tuple[str, int, list[int]]:
    """逐頁讀 PDF：有文字層的頁直接抽文字，掃描頁才算圖送 vision 模型。

    整份都有文字層時完全不會連 vision endpoint（ocr_pages 為空）。

    Returns:
        (text, pages, ocr_pages)：合併後全文、實際處理頁數、走了視覺辨識的頁碼。
    """
    import fitz  # pymupdf

    texts: list[str] = []
    ocr_pages: list[int] = []
    doc = fitz.open(path)
    try:
        for number in range(1, min(doc.page_count, max_pages) + 1):
            page = doc[number - 1]
            text = page.get_text("text").strip()
            if len(text) < TEXT_LAYER_MIN_CHARS:
                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                text = _call_vision(_to_data_url(pix.tobytes("png"), "image/png"), prompt)
                ocr_pages.append(number)
            texts.append(text)
    finally:
        doc.close()
    return _join_pages(texts), len(texts), ocr_pages


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


def _read_document(path: str, prompt: str, max_pages: int) -> dict:
    """讀一個檔案，回傳 text／pages／ocr_pages／mime 欄位。

    PDF 走逐頁 `_extract_pdf`（文字層優先）；其餘格式維持原本的單張圖片辨識。

    Args:
        path: 檔案路徑。
        prompt: 視覺辨識提示（只有真的要辨識的頁才用得到）。
        max_pages: PDF 最多處理幾頁。

    Returns:
        可直接併進輸出 JSON 的欄位 dict。
    """
    if Path(path).suffix.lower() == ".pdf":
        text, pages, ocr_pages = _extract_pdf(path, prompt, max_pages)
        return {"text": text, "pages": pages, "ocr_pages": ocr_pages, "mime": "application/pdf"}
    data_url, mime = _image_data_url(path)
    return {"text": _call_vision(data_url, prompt), "pages": 1, "ocr_pages": [1], "mime": mime}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, help="圖片或 PDF 路徑")
    parser.add_argument("--prompt", default="", help="辨識提示（選填）")
    parser.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAX_PAGES,
        help=f"PDF 最多處理幾頁（預設 {DEFAULT_MAX_PAGES}）",
    )
    parsed = parser.parse_args()

    started = time.time()
    path = parsed.path
    prompt = parsed.prompt.strip() or DEFAULT_PROMPT
    max_pages = max(1, parsed.max_pages)

    if not Path(path).exists():
        print(json.dumps({"ok": False, "error": f"檔案不存在: {path}"}, ensure_ascii=False))
        return

    image_hash = _sha256_file(path)
    # max_pages 進 key：同一份 PDF 用不同上限讀出來的內容不一樣，不能共用快取。
    cache_key = f"{image_hash}-{_short_hash(prompt)}-p{max_pages}"

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
        fields = _read_document(path, prompt, max_pages)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return

    result = {
        "ok": True,
        **fields,
        "image_hash": image_hash,
        "cache_key": cache_key,
        "cache_hit": False,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    _write_cache(cache_key, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
