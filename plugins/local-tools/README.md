# local-tools Plugin

Hermes agent 本機工具包，從 `~/alice-tools-pack/` 移植而來，以 hermes standalone plugin 形式整合七個自訂工具。

---

## 目錄結構

```
~/.hermes/plugins/local-tools/
├── plugin.yaml          # plugin 宣告（kind: standalone）
├── __init__.py          # 工具註冊進入點
├── schemas.py           # 六個工具的 OpenAI function-calling schema
├── tools.py             # 工具 handler 與 subprocess 呼叫邏輯
├── README.md            # 本文件
└── scripts/             # 從 alice-tools-pack 複製的腳本（唯一執行來源）
    ├── hr/
    │   ├── alice-payroll-engine.py
    │   ├── alice-payroll-config.2026.tw.json
    │   ├── alice-payroll-engine.sh
    │   ├── alice-payroll-engine.test.py
    │   ├── sample-employees.csv
    │   └── sample-attendance.csv
    ├── law/
    │   ├── alice-tw-law-local.py          # ⚠️ 有修改（SSL fix）
    │   ├── alice-tw-law-local.sh
    │   └── alice-tw-law-local.test.py
    ├── math/
    │   ├── alice-engineering-calculator.py
    │   ├── alice-engineering-calculator.sh
    │   └── alice-engineering-calculator.test.py
    ├── memory/
    │   ├── alice-long-term-memory.py
    │   └── alice-long-term-memory.sh
    ├── research/
    │   ├── alice-assistant-ecosystem.py
    │   ├── alice-assistant-ecosystem.sh
    │   └── china-ai-assistant-index.json
    ├── image-ocr/
    │   └── alice-image-exam-ocr.py
    └── browser/
        ├── alice-browser-task.py          # ⚠️ 有修改（截圖清理機制）
        ├── alice-browser-task.sh
        └── tw-ecommerce-index.json
```

Plugin 的執行資料（SQLite DB、法規快取、截圖等）統一放在：

```
~/.hermes/local-tools-data/
├── law-data/
│   ├── tw-law.sqlite        # 法規全文搜尋資料庫
│   └── raw/                 # 從 law.moj.gov.tw 下載的原始 ZIP/JSON
├── memory/
│   └── alice-memory.sqlite  # 長期記憶 SQLite
├── image-ocr-cache/         # OCR 辨識結果快取（以 SHA-256 + prompt hash 為 key）
└── browser/
    ├── firefox-profile/     # Firefox 使用者設定檔
    ├── screenshots/         # 自動截圖（超過 ALICE_BROWSER_SS_MAX_DAYS 天自動清除）
    └── browser.lock         # 單一實例鎖
```

---

## 工具名稱對照

移植時依功能重新命名，並刻意避開 hermes 內建工具名稱（`memory`、`browser`）：

| hermes 工具名 | 原始腳本目錄 | 說明 |
|---|---|---|
| `hr` | `alice-tools-pack/hr/` | 台灣薪資試算 |
| `law` | `alice-tools-pack/law/` | 台灣法規查詢 |
| `math` | `alice-tools-pack/math/` | 工程數學計算機 |
| `longmem` | `alice-tools-pack/memory/` | 本機長期記憶（原名 `memory`，與 hermes 內建衝突改名） |
| `research` | `alice-tools-pack/research/` | AI 助理生態系查詢 |
| `image_ocr` | `alice-tools-pack/image-ocr/` | 圖片／PDF OCR，呼叫 vision API（Qwen2.5-VL） |
| `webdriver` | `alice-tools-pack/browser/` | 瀏覽器自動化（原名 `browser`，與 hermes 內建衝突改名） |

> **注意：** hermes 的 `tools list` 指令顯示的 `memory` 和 `browser` 是 hermes 內建工具，不是本 plugin 的工具。

---

## 各檔案說明

### `plugin.yaml`

```yaml
name: local-tools
version: 1.0.0
kind: standalone
```

`kind: standalone` 表示此 plugin 不自動啟用，需在 `~/.hermes/config.yaml` 的 `plugins.enabled` 清單內加入 `local-tools` 才會載入。

---

### `__init__.py`

Plugin 進入點。hermes 在啟動時呼叫 `register(ctx)`，將七個工具逐一以 `ctx.register_tool()` 註冊進 `local_tools` toolset。

`webdriver` 工具有 `check_fn=check_browser_available`，若 `geckodriver` 未安裝則自動從工具清單排除，不影響其他工具。

另外在 `register()` 最後會以 `ctx.register_hook("pre_llm_call", pre_llm_call_ocr_hook)` 掛載 `image_ocr` 的自動辨識 hook。

---

### `schemas.py`

六個工具的 OpenAI function-calling schema，供 hermes 在每次對話開始時注入 model context。格式為：

```python
{
    "description": "...",
    "parameters": {
        "type": "object",
        "properties": { ... },
        "required": [...]
    }
}
```

**注意事項：**
- `research` 工具的 `command` 說明中特別強調 `search` 可搜尋 capabilities 欄位，而 `list` 僅能按大分類篩選，用以引導 model 選擇正確指令。
- `webdriver` 的 `command` 包含 `cleanup`（截圖清理），搭配選填參數 `days`。

---

### `tools.py`

Handler 實作與 subprocess 呼叫邏輯。幾個關鍵設計：

**Handler 簽名**

hermes registry 以 `handler(args_dict, **kwargs)` 呼叫 handler（不是 `handler(**args_dict)`），因此所有 handler 的第一個參數都是 `args: dict`，需自行 `.get()` 取值：

```python
def handle_math(args: dict, **_: Any) -> str:
    expression = args.get("expression", "")
    return _run(_MATH_SCRIPT, [expression])
```

**腳本路徑**

```python
TOOLS_ROOT = Path(__file__).parent / "scripts"
```

所有腳本路徑基於 `TOOLS_ROOT`，不使用硬碼絕對路徑，也不依賴 `~/alice-tools-pack/`。

**環境變數覆蓋**

原始腳本有硬碼舊路徑（`/home/alice_gx10/.alice/...`），透過在 subprocess 環境中覆蓋對應變數解決，原始腳本不修改：

```python
_BASE_ENV = {
    **os.environ,
    "ALICE_TW_LAW_DATA_DIR": str(_PLUGIN_DATA / "law-data"),
    "ALICE_TW_LAW_DB":       str(_PLUGIN_DATA / "law-data" / "tw-law.sqlite"),
    "ALICE_MEMORY_DB":       str(_PLUGIN_DATA / "memory" / "alice-memory.sqlite"),
}
```

Plugin 資料根目錄：`$HERMES_HOME/local-tools-data/`（預設 `~/.hermes/local-tools-data/`）。

---

## `image_ocr` 工具說明

### 腳本

`scripts/image-ocr/alice-image-exam-ocr.py`（直接從 `alice-tools-pack/image-ocr/` 移植，未修改）

### 功能

呼叫本機 vision API（預設 Qwen2.5-VL）辨識圖片或 PDF 的文字與內容：

- 支援格式：`.jpg`、`.jpeg`、`.png`、`.webp`、`.pdf`
- PDF 自動取第一頁，以 2× 解析度轉成 PNG 後辨識（需要 `pymupdf`）
- 辨識結果以 `SHA-256（檔案內容）+ SHA-256（prompt）` 為 key 快取到 `~/.hermes/local-tools-data/image-ocr-cache/`，相同檔案＋相同 prompt 不重複呼叫 API
- 回傳 JSON：`{ ok, text, image_hash, cache_key, cache_hit, mime, elapsed_ms }`

### 設定來源

Vision API 的端點與 model 從 `~/.hermes/config.yaml` 讀取：

```yaml
auxiliary:
  vision:
    base_url: http://127.0.0.1:8001/v1   # 預設值
    model: qwen2.5-vl                     # 預設值
```

`OPENAI_API_KEY` 優先從 `~/.hermes/.env` 讀取，其次繼承 shell 環境變數。

可覆蓋的環境變數（`tools.py` 中的 `_OCR_ENV`）：

| 環境變數 | 說明 | 預設值 |
|---|---|---|
| `ALICE_VISION_CHAT_URL` | Vision API endpoint | 由 `config.yaml` 讀取，fallback `http://127.0.0.1:8001/v1/chat/completions` |
| `ALICE_VISION_MODEL` | 模型名稱 | 由 `config.yaml` 讀取，fallback `qwen2.5-vl` |
| `ALICE_IMAGE_OCR_CACHE_DIR` | 快取目錄 | `~/.hermes/local-tools-data/image-ocr-cache/` |
| `OPENAI_API_KEY` | API 金鑰 | 從 `~/.hermes/.env` 讀取 |

### `pre_llm_call` hook — 自動 PDF OCR

`tools.py` 中的 `pre_llm_call_ocr_hook` 會在每次 LLM 收到訊息前執行。若訊息包含 `It is saved at: /path/to/file.pdf` 格式的字串（hermes 上傳附件後的標準格式），hook 會自動呼叫 OCR 並將辨識結果以 context 注入，讓 LLM 直接讀取文字內容，不需要手動呼叫 `image_ocr` 工具。

---

## Scripts 修改說明

原始腳本複製後有以下兩處修改：

### `scripts/law/alice-tw-law-local.py`

**問題：** `law.moj.gov.tw` 的 TLS 憑證缺少 Subject Key Identifier，Python 3.13 預設拒絕。

**修改：** 在 `download_endpoint()` 函式使用自訂 SSL context，跳過憑證驗證（僅限 mirror 下載，資料為公開政府法規）：

```python
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# download_endpoint() 內：
with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as response, ...:
```

### `scripts/browser/alice-browser-task.py`

**新增：截圖自動清理機制**

每次截圖前自動清除超過 `ALICE_BROWSER_SS_MAX_DAYS` 天（預設 7 天）的舊截圖，防止硬碟空間持續累積：

```python
SS_MAX_DAYS = int(os.environ.get("ALICE_BROWSER_SS_MAX_DAYS", "7"))

def _prune_screenshots(max_age_days=SS_MAX_DAYS) -> int:
    # 刪除 SCREENSHOT_DIR 下超過 max_age_days 天的 .png
    ...

def screenshot_path(label: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _prune_screenshots()   # ← 新增：每次截圖前清理
    return SCREENSHOT_DIR / f"{int(time.time())}-{sanitize_name(label)}.png"
```

**新增：`cleanup` 子指令**

可手動觸發截圖清理（不需要啟動瀏覽器）：

```bash
python3 alice-browser-task.py cleanup --days 3
# 回傳：{"ok": true, "removed": N, "remaining": M, ...}
```

透過 hermes 使用：`webdriver` 工具 `command=cleanup`，選填 `days` 參數。

---

## 安裝步驟

### 1. 系統依賴

```bash
# math 工具需要 sympy
pip3 install sympy --break-system-packages

# image_ocr 工具需要 pymupdf（PDF 轉圖支援）
pip3 install pymupdf --break-system-packages

# webdriver 工具需要 Firefox + Selenium + geckodriver
sudo apt-get install -y firefox-esr
pip3 install selenium --break-system-packages

# geckodriver（從 GitHub 下載最新版）
GECKO_VER=$(curl -s https://api.github.com/repos/mozilla/geckodriver/releases/latest \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
curl -sL "https://github.com/mozilla/geckodriver/releases/download/${GECKO_VER}/geckodriver-${GECKO_VER}-linux64.tar.gz" \
  | sudo tar -xz -C /usr/local/bin/
sudo chmod +x /usr/local/bin/geckodriver
```

### 2. 啟用 plugin

在 `~/.hermes/config.yaml` 的 `plugins.enabled` 加入 `local-tools`：

```yaml
plugins:
  enabled:
    - local-tools
    # ...其他 plugin
```

### 3. 初始化法規資料庫

首次使用前需要從 law.moj.gov.tw 下載法規資料（約需 1–2 分鐘，需要網路）。透過 hermes 觸發：

```
請用 law 工具執行 mirror 指令
```

或直接執行：

```bash
ALICE_TW_LAW_DATA_DIR=~/.hermes/local-tools-data/law-data \
ALICE_TW_LAW_DB=~/.hermes/local-tools-data/law-data/tw-law.sqlite \
python3 ~/.hermes/plugins/local-tools/scripts/law/alice-tw-law-local.py mirror
```

下載完成後會顯示：`"laws": 14939, "articles": 239489`（數字依更新時間略有不同）。

---

## 驗證測試

### 直接 Python 測試

```bash
SCRIPTS=~/.hermes/plugins/local-tools/scripts

# math
python3 "$SCRIPTS/math/alice-engineering-calculator.py" "integrate sin(x) from 0 to pi"
# 預期：{"ok": true, "answer": "2", ...}

# research
python3 "$SCRIPTS/research/alice-assistant-ecosystem.py" \
  --index "$SCRIPTS/research/china-ai-assistant-index.json" --json list
# 預期：{"ok": true, "projects": [...]} 共 12 筆

# longmem（memory）
ALICE_MEMORY_DB=~/.hermes/local-tools-data/memory/alice-memory.sqlite \
python3 "$SCRIPTS/memory/alice-long-term-memory.py" \
  remember --user-id alice --text "測試記憶" --type preference
# 預期：{"ok": true, "event": "create", ...}

# hr
python3 "$SCRIPTS/hr/alice-payroll-engine.py" generate \
  --employees "$SCRIPTS/hr/sample-employees.csv" \
  --attendance "$SCRIPTS/hr/sample-attendance.csv" \
  --out /tmp/payroll-test.xlsx \
  --config "$SCRIPTS/hr/alice-payroll-config.2026.tw.json"
# 預期：{"ok": true, "employees": 4, ...}

# law（需先 mirror）
ALICE_TW_LAW_DATA_DIR=~/.hermes/local-tools-data/law-data \
ALICE_TW_LAW_DB=~/.hermes/local-tools-data/law-data/tw-law.sqlite \
python3 "$SCRIPTS/law/alice-tw-law-local.py" search "勞基法第84條" --limit 3
# 預期：{"ok": true, "count": 3, "results": [...]}

# image_ocr（需要本機 vision API 正在執行，且有圖片或 PDF 檔案）
ALICE_IMAGE_OCR_CACHE_DIR=~/.hermes/local-tools-data/image-ocr-cache \
ALICE_VISION_CHAT_URL=http://127.0.0.1:8001/v1/chat/completions \
ALICE_VISION_MODEL=qwen2.5-vl \
python3 "$SCRIPTS/image-ocr/alice-image-exam-ocr.py" --path /path/to/test.jpg
# 預期：{"ok": true, "text": "...", "cache_hit": false, "elapsed_ms": ...}

# 相同檔案第二次呼叫：
# 預期：{..., "cache_hit": true, "elapsed_ms": <極短>}

# webdriver（browser）
ALICE_BROWSER_HOME=~/.hermes/local-tools-data/browser \
ALICE_BROWSER_LOCK=~/.hermes/local-tools-data/browser/browser.lock \
ALICE_BROWSER_PROFILE=~/.hermes/local-tools-data/browser/firefox-profile \
ALICE_BROWSER_SCREENSHOTS=~/.hermes/local-tools-data/browser/screenshots \
ALICE_ECOMMERCE_INDEX="$SCRIPTS/browser/tw-ecommerce-index.json" \
ALICE_GECKODRIVER_BIN=/usr/local/bin/geckodriver \
python3 "$SCRIPTS/browser/alice-browser-task.py" health
# 預期：{"ok": true, "status": "ready", ...}
```

### hermes 測試

重啟 hermes 後，提示 model 明確指定工具與 command（qwen3-next 等較弱的 model 需要明確指示）：

```
呼叫 math 工具，expression="integrate sin(x) from 0 to pi"，回傳原始結果

呼叫 law 工具，command=search，query="勞基法第84條"，回傳原始結果

呼叫 longmem 工具，command=remember，user_id=alice，text="測試"，回傳原始結果

呼叫 hr 工具，employees_csv=<路徑>，attendance_csv=<路徑>，out_xlsx=/tmp/test.xlsx，回傳原始結果

呼叫 webdriver 工具，command=health，回傳原始結果

呼叫 image_ocr 工具，path="/path/to/test.jpg"，回傳原始結果
```

---

## 常見問題 QA

**Q：改了 plugin 檔案後要怎麼讓 hermes 生效？**
A：重啟 hermes（關掉 `hermes chat` 再重開）。hermes 啟動時才載入 plugin 模組，執行中修改檔案不會即時生效。

**Q：law mirror 失敗，出現 SSL 相關錯誤？**
A：已在 `scripts/law/alice-tw-law-local.py` 內處理（SSL context 跳過憑證驗證）。若仍失敗，確認 Python 版本是 `/usr/bin/python3`（3.13），以及網路可連外。

**Q：webdriver 工具回報 geckodriver 找不到？**
A：確認 `which geckodriver` 有輸出。若無，重新執行安裝步驟的 geckodriver 下載指令。`check_browser_available()` 只檢查 `geckodriver` 是否在 PATH 內。

**Q：longmem 工具回傳的記憶和 hermes 內建 memory 有關聯嗎？**
A：完全獨立。本 plugin 的長期記憶存在 `~/.hermes/local-tools-data/memory/alice-memory.sqlite`，hermes 內建 memory 是另一套系統，兩者互不干擾。

**Q：research 工具問「列出支援 memory 功能的助理」，model 卻說沒有？**
A：model 可能錯誤選擇了 `list` 指令（回傳所有專案，需 model 自行解讀 capabilities 欄位），而非 `search memory`（直接篩選結果）。解法是明確告知 model 使用 `search` 指令，或換用 tool use 能力較強的 model（如 Claude、GPT-4o）。

**Q：hermes 啟動時 log 出現 `Tool registration REJECTED: 'memory'`？**
A：這是正常的。hermes 內建的 `memory` toolset 嘗試重新註冊同名工具時被擋下，表示我們的 `longmem`（原名 `memory`）已先成功註冊。兩者互不影響。若看到 `longmem` 或 `webdriver` 相關的 REJECTED 訊息才需要處理。

**Q：截圖累積太多怎麼清？**
A：透過 hermes 呼叫 `webdriver` 工具，`command=cleanup`，選填 `days` 指定幾天前的截圖要清除（預設 7 天）。也可以設定環境變數 `ALICE_BROWSER_SS_MAX_DAYS` 改變預設值，每次截圖時會自動觸發清理。

**Q：image_ocr 工具回傳 `ModuleNotFoundError: No module named 'fitz'`？**
A：`fitz` 是 `pymupdf` 套件。執行 `pip3 install pymupdf --break-system-packages` 安裝。純圖片格式（jpg/png/webp）不需要此套件，僅 PDF 辨識時才用到。

**Q：image_ocr 工具回傳 `ConnectionRefusedError` 或 `URLError`？**
A：vision API server 未啟動。確認 Qwen2.5-VL（或 `~/.hermes/config.yaml` 中設定的模型）正在監聽對應 port。直接 `curl http://127.0.0.1:8001/v1/models` 確認是否有回應。

**Q：PDF 辨識只取第一頁，如何辨識多頁？**
A：目前腳本設計為單頁辨識（`doc[0]`），多頁 PDF 需分頁處理。可多次呼叫 `image_ocr` 工具並傳入不同頁數，或直接修改腳本。

**Q：快取占用空間過大怎麼清？**
A：快取存在 `~/.hermes/local-tools-data/image-ocr-cache/`，直接刪除 `.json` 檔案即可。每筆快取以辨識文字為主，通常很小，但大量圖片的 base64 content 不存入快取，僅存 OCR 結果文字。

**Q：pre_llm_call hook 沒有自動 OCR？**
A：hook 只比對訊息中 `It is saved at: <path>.pdf` 格式的字串（hermes 附件上傳的標準格式）。若 PDF 路徑是以其他格式傳入，hook 不會觸發，需手動呼叫 `image_ocr` 工具。

**Q：hermes 說工具執行失敗，但直接 Python 測試是正常的？**
A：檢查 `hermes logs` 是否有 `Tool registration REJECTED` 或 `TypeError`。常見原因：（1）hermes 未重啟、舊版 plugin 仍在記憶體中；（2）toolset `local_tools` 未啟用；（3）model 未實際呼叫工具而是自行回答。

---

## 已知限制

- **`webdriver` 工具不支援有 display 的模式**：目前僅 headless 模式，在有 GUI 的環境可加 `--headed` 參數手動測試，但透過 hermes 呼叫時固定 headless。
- **Model 依賴**：qwen3-next 等較小的 model 在 tool use 指令選擇上不穩定，建議配合 Claude Sonnet/Opus 或 GPT-4o 使用以獲得最佳體驗。
- **`law mirror` 需要網路**：法規資料庫需要定期手動更新（`law` 工具 `command=mirror`），無自動排程。
- **`image_ocr` PDF 辨識僅限第一頁**：多頁 PDF 需多次呼叫或手動修改腳本。
- **`image_ocr` 依賴本機 vision API**：需要 Qwen2.5-VL（或 `config.yaml` 設定的模型）在本機運行，無法在沒有 GPU 的環境使用。
