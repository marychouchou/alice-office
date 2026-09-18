"""local-tools plugin — Alice 本機工具包。

Registers 8 tools into the 'local_tools' toolset:
  hr         台灣薪資試算 (alice-tools-pack/hr/)
  law        台灣法規查詢 (alice-tools-pack/law/)
  math       工程數學計算機 (alice-tools-pack/math/)
  longmem    本機長期記憶 (alice-tools-pack/memory/)
  research   AI 助理生態系查詢 (alice-tools-pack/research/)
  image_ocr  PDF／圖片讀取（文字層優先、掃描頁走視覺模型） (alice-tools-pack/image-ocr/)
  share_file 把產出的檔案交給使用者下載（回傳 outbox:// 佔位連結，router 改寫）
  webdriver  瀏覽器自動化 (alice-tools-pack/browser/；依賴未安裝時 check_fn=False)

工具名稱刻意避開 hermes 內建的 'memory' 與 'browser'，防止撞名。
"""

from .schemas import (
    HR_SCHEMA,
    LAW_SCHEMA,
    MATH_SCHEMA,
    LONGMEM_SCHEMA,
    OCR_SCHEMA,
    RESEARCH_SCHEMA,
    SHARE_FILE_SCHEMA,
    WEBDRIVER_SCHEMA,
)
from .tools import (
    handle_hr,
    handle_law,
    handle_math,
    handle_longmem,
    handle_ocr,
    handle_research,
    handle_share_file,
    handle_webdriver,
    check_browser_available,
)

# share_file's check_fn is None on purpose: whether a download link can
# actually be served depends on the router's PUBLIC_BASE_URL, which this
# container has no way to see. Any check here would be a guess; the router
# replaces the placeholder with an honest "not configured" notice instead.
_TOOLS = (
    ("hr",         HR_SCHEMA,         handle_hr,         None,                    "💰"),
    ("law",        LAW_SCHEMA,        handle_law,        None,                    "⚖️"),
    ("math",       MATH_SCHEMA,       handle_math,       None,                    "🔢"),
    ("longmem",    LONGMEM_SCHEMA,    handle_longmem,    None,                    "🧠"),
    ("research",   RESEARCH_SCHEMA,   handle_research,   None,                    "🔭"),
    ("image_ocr",  OCR_SCHEMA,        handle_ocr,        None,                    "🖼️"),
    ("share_file", SHARE_FILE_SCHEMA, handle_share_file, None,                    "📎"),
    ("webdriver",  WEBDRIVER_SCHEMA,  handle_webdriver,  check_browser_available, "🌐"),
)


def register(ctx) -> None:
    for name, schema, handler, check_fn, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="local_tools",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )
