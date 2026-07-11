## Repo purpose

此專案讓每一個使用者可以透過Line(一個台灣常用的通訊撋體)來access他自己的AI agent企業助理。
AI agent 以hermes agent [https://hermes-agent.nousresearch.com/docs]當作框架，每一個chat room 及配置一個AI agent。我們也用chat room來當作是隔離的概念。chat room A完全沒辦法access, chat room A。
我們希望使用者在使用Line時，完全就像在使用自己部署的hermes AI agent。
## Stack

- **Language**: Python 3.12
- **Framework**: FastAPI
- **Package Manager**: uv（lockfile: `uv.lock`）
- **Linting**: Ruff（linter + formatter）、mypy（strict mode）
- **Testing**: pytest、pytest-asyncio

## Architecture

LINE Official Account webhook router。
接收 LINE Platform 的 request，依據事件類型（message、follow、postback 等）分派給對應的 container 處理。
LINE Platform → FastAPI Router → Container A

→ Container B

→ Container C

## Hermes Container Model

- 每個 LINE 聊天室對應一個獨立的 Hermes Agent container（`hermes_<room_id>`），由
  `container_manager.py` 動態建立，不是寫死的固定 container。
- **`/opt/data` = Hermes 的 `HERMES_HOME`**（官方環境變數，見
  `NousResearch/hermes-agent` 的 `get_hermes_home()`）。每個房間的 `data/<room_id>/`
  bind mount 到這裡（rw，每房間各自一份，容器間互不共用）。
- **Hermes 自己的 `gateway run` 每次開機都會把這個目錄補完整**：`sessions/`、
  `skills/`、`kanban.db`、`state.db`、`logs/`、各種 `.lock` 都是 Hermes 自己寫的，
  不是這個 repo 寫的。`skills/` 每次開機會做 manifest-based sync（`.bundled_manifest`
  記錄來源 hash），把 image 內建 skill 複製進來但跳過使用者已改過的——房間可以直接
  編輯 `data/<room_id>/skills/<name>/SKILL.md` 客製化，不會被下次開機蓋掉。
- 這個 repo 只負責 write-once 的初始化：`config.yaml`（`_ensure_config_yaml`）、
  MCP／plugin 原始碼（`_ensure_mcp_seed` / `_ensure_plugin_seed`，從
  `src/hermes/{mcp,plugin}/` seed 到 `data/<room_id>/{mcp,plugins}/`）——都只在房間
  第一次建立時寫一次，之後永不覆蓋，讓房間可以自由編輯自己的副本；改 repo 樣板只影響
  之後新建立的房間。除此之外 `data/<room_id>/` 底下其他所有東西都是 Hermes 執行期
  自己長出來的。
- **沒有熱載入**：改 `config.yaml`／skills／mcp／plugins 都要
  `docker restart hermes_<room_id>` 才會生效。
- MCP 是 Node ESM，依賴解析靠從檔案位置往上找 `node_modules`（ESM 不吃
  `NODE_PATH`），所以共用依賴烤在 image 的 `/opt/node_modules`，不放進各房間自己的
  `mcp/<name>/` 底下（見 `Dockerfile.hermes`）。
- Python 第三方套件（sympy／pymupdf／selenium）跟 hermes-agent 自己的 venv 隔離，烤在
  獨立的 `/opt/tools/.venv`（由 `src/hermes/runtime/pyproject.toml` + `uv.lock` 驅動）：
  plugin 進程用 `TOOLS_PYTHON` 環境變數解析到這個 venv，skill 的 terminal session 用
  `tools-python` 指令；`/opt/hermes/.venv`（hermes-agent 自己的 venv）只留 `pyyaml`
  給 in-process 的 plugin 層（`tools.py` 讀 `config.yaml`）用。

## Commands

- `uv sync` — 安裝依賴
- `uv run fastapi dev` — 啟動開發伺服器（localhost:8000）
- `uv run pytest` — 執行所有測試
- `uv run pytest tests/test_foo.py::test_bar -v` — 執行單一測試
- `uv run pytest --cov=src --cov-report=term-missing` — 含覆蓋率
- `uv run mypy src/` — 型別檢查
- `uv run ruff check .` — lint 檢查
- `uv run ruff format .` — 格式化

commit前必跑：`uv run ruff check . && uv run mypy src/ && uv run pytest`
在實作過程中 linting and formatting都不重要
## Coding Conventions

- 所有函式簽名必須有型別提示（參數與回傳值）
- 每個模組頂部加 `from __future__ import annotations`
- 使用 `pathlib.Path` 取代 `os.path`
- 字串格式化統一用 f-string
- 函式長度上限 30 行，超過則抽出子函式
- 不使用 `Any` 型別，除非絕對必要
- 公開函式使用 Google style docstring

## Growth Discipline

codebase 變大時的結構規則。每一條都是「訊號 → 動作」，看到訊號就做，不需要憑感覺判斷「夠不夠乾淨」。

### 門檻訊號

- **Rule of Three**：相同邏輯第 2 次出現 → 允許複製，但在兩處都留註解指向彼此；
  第 3 次出現 → 抽成共用函式/模組。第 1 次出現時不要預先抽象。
- **對同一個值的 if/elif 達 4 個分支**（如 LINE `msg_type`、MCP tool `name`、plugin
  `command`）→ 改成 dict dispatch table（`{key: handler}`）。3 個以下維持 if/elif。
  現況基準：`router.py::_resolve_inbound_text` 對 `msg_type` 是 4 個分支，剛好在門檻上
  ——下次新增 LINE message type 時先改成 dispatch table，不要加第 5 個 if。
- **單一檔案超過 400 行** → 檢查是否混了兩種以上「改動理由」（用下方路由表的分類判斷）。
  有混 → 依改動理由拆檔；沒混（單一主題只是長，如 `google_oauth.py` 396 行整檔都是
  OAuth）→ 不動。
- **同一個 `data/<room_id>/` 子路徑在兩個以上模組各自拼字串** → 提升成 `config.py`
  Settings 的 method（比照 `room_google_dir` 的做法），不要留兩份拼法。
- **函式內控制結構巢狀超過 3 層**（如 if 裡 for 裡 try 裡 if；elif 鏈不算加一層）→
  用 early return／guard clause／抽子函式打平到 3 層以內。現況超標的函式清單見
  Known Anti-Patterns 第 9 條。

### 消除特殊情況（寫完函式後的檢查點）

這不是要求現在重構既有程式碼，是**新增程式碼時**的檢查點：寫完一個函式後，數一下
裡面有幾個「這是特殊情況所以要分開處理」的分支（`if is_first_time`、
`if not exists then create else use`、`if xxx_enabled` 這類）。**超過一個**就先想：
能不能靠改資料結構或初始化方式讓正常路徑直接涵蓋它，而不是保留 if 繞過去。這是
Linus Torvalds 講鏈結串列刪除節點時的 "good taste" 精神——特殊情況的數量反映的是
資料結構／介面設計得好不好，不是要求風格模仿。

- repo 內做對的例子：`google_oauth._load_tokens` 對不存在的檔案直接回 `{}`，
  所以所有呼叫端都沒有「tokens.json 還沒建立」的分支。新的讀取類 helper 比照辦理
  （在邊界把缺失正規化掉，讓呼叫端只有一條路徑）。
- 邊界提醒：`_seed_templates` 的 `if dest_dir.exists(): continue` 是 write-once
  語意本身（見 Hermes Container Model），不是待消除的特殊情況——不要「修」它。

### 新程式碼放哪裡（路由表）

依「這段程式碼將來會因為什麼原因被改動」決定位置：

| 改動理由 | 放這裡 |
|---|---|
| LINE wire format（event 結構、訊息型別解析、簽章、送訊長度/則數限制） | `src/alice_office_router/line_*.py` |
| container 生命週期（建立/啟動/健康等待/URL 解析） | `container_manager.py`——**docker SDK 只允許在這個檔案 import**（現況已如此，維持住） |
| 房間 write-once seed（複製 template 到 `data/<room_id>/`） | 目前在 `container_manager.py`；要新增 seed 種類時，先把 seed 函式群抽成 `room_seed.py` 再加 |
| 「這則訊息該不該進 agent」的 gate 判斷（如 Google OAuth gate） | 獨立模組提供回傳 status 的純函式（比照 `google_oauth.check_google_authorization`），router 只呼叫、不寫判斷內容 |
| 對 Hermes agent 的 HTTP 協定 | `hermes_client.py` |
| 環境變數與路徑推導 | `config.py` 的 Settings |
| 新的 agent 能力（工具） | `src/hermes/mcp/<name>/` 或 `src/hermes/plugin/` 的 template，不是 router 的功能 |

### Known Anti-Patterns（2026-07-12 掃描）

改到這些檔案時適用；每條都寫明觸發時機和該做的事：

1. **`src/hermes/mcp/gmail/token_manager.py` 和 `src/hermes/mcp/drive/token_manager.py`
   是逐 byte 相同的複本**（117 行）。這是 per-room seeding 以 template 目錄為單位的
   結構性結果，不是意外——但修其中一個的 bug 必須同一個 commit 同步改另一個。
   若出現第三個需要 token_manager 的 Python MCP：不要複製第三份，改烤進 image 的
   共用路徑（比照 `/opt/node_modules` 處理 Node 依賴的方式）。
2. **secretary MCP 的 JSON store 樣板已重複 3 次**：`todo.mjs`／`attendance.mjs`／
   `expense.mjs` 各有一份 `readStore`/`writeStore`/`get*`/`save*`；`ok()` helper 在
   8 個 `tools/*.mjs` 各一份。已達 Rule of Three：下一個需要 per-user JSON store 的
   secretary tool 出現時，先抽 `tools/_store.mjs`（連同 `ok()`），不要複製第 4 份。
3. **`gmail/server.py`（8 個）和 `drive/server.py`（10 個）的 `call_tool` 是連續
   `if name ==` 鏈**，已超過 dispatch table 門檻。下次在任一檔新增 tool 時，先改成
   `{name: handler}` dict 再加新 tool。
4. **`local-tools/tools.py` 有 4 個 handler（law／longmem／research／webdriver）在做
   同一種 `command` → argv 的 if/elif 翻譯**。第 5 個多 command handler 出現時，
   抽成表驅動的 `{command: argv_builder}` 共用寫法。
5. **`container_manager.py`（622 行）混了三種改動理由**：docker 生命週期、write-once
   seed（`_seed_templates`／`_ensure_*_seed`／`ensure_google_seed`）、config.yaml
   渲染（`_format_*`／`_load_mcp_manifest`／`_ensure_config_yaml`）。要新增 seed 種類
   或 config 渲染邏輯前先拆檔（seed → `room_seed.py`，渲染 → `hermes_config.py`），
   container 生命週期留在原檔；只是修 bug 則不必拆。
6. **`router.py` 的 `_resolve_inbound_text`／`_download_and_note_media` 和
   sticker／location 的中文 placeholder 是 LINE 訊息格式層邏輯，長在 router 層**
   （違反「router 只做分派」）。現況可用；下次要新增 message type 或改任何
   placeholder 文字時，先把這組函式搬到 `line_inbound.py` 再改。
7. **特殊情況散落：`config.google_oauth_enabled` 的 if 出現在 5 處**——
   `container_manager.py` 的 `_ensure_mcp_seed`／`ensure_google_seed`／
   `_build_volume_config`，加上 `google_oauth.py` 的 `oauth_start`／
   `check_google_authorization`。「這個部署沒啟用 Google」這一個特殊情況，房間
   初始化流程的每一站都得各自記得檢查，漏一站就是 bug。現況可用；第二個需要
   OAuth gate 的整合（如 Microsoft）出現時，不要複製第二組散落的 `xxx_enabled`
   if——把房間初始化改成一張步驟清單（seed 步驟、mount 步驟、gate 檢查登記
   進去），讓「未啟用」＝不在清單上，而不是每站一個 if。
8. **特殊情況旗標：`get_or_create_container` 的 `needs_wait`**
   （`container_manager.py`）。running／stopped／missing 三條路徑用一個布林旗標
   記住「剛剛走了哪條」，只為了決定要不要等健康檢查。`_wait_until_ready` 對健康
   的 container 第一次 poll 就返回——永遠呼叫它即可消掉旗標和分支（代價是每則
   訊息多一次容器內 HTTP GET）。下次改這個函式時順手消掉。
9. **超過 3 層巢狀的函式（2026-07-12 AST 實測，均為 4 層）**：
   `container_manager.py::_wait_until_ready`（with→while→try→if）、
   `src/hermes/mcp/gmail/token_manager.py::get_access_token`（if→for→try→if；
   drive 的逐 byte 複本同，見第 1 條）、
   `src/hermes/plugin/local-tools/scripts/law/alice-tw-law-local.py` 的
   `find_law_records` 與 `import_json`。下次改到任一個時用 early return／
   抽子函式打平到 3 層以內；不必為打平專門開 PR。`.mjs` 檔掃描後無超標案例。

## Error Handling

- 禁止裸 `except:`，必須捕捉具體例外
- 禁止靜默吞掉例外，必須記錄 log
- 使用 context manager（`with`）管理資源
- 使用 `logger.error` 取代 `print` 報告錯誤
- LINE webhook 驗簽失敗必須回傳 400，不可靜默忽略

## Testing

- 遵循 Arrange-Act-Assert 結構
- mock 所有外部依賴（LINE API、下游 container）
- 測試輸出資料夾加入 `.gitignore`

## Security

- LINE Channel Secret 與 Access Token 只存 `.env`
- 確保 `.env` 在 `.gitignore`
- 每個 webhook request 必須驗證 LINE 簽章（`x-line-signature`）

## Git

- Conventional commits：`feat:`, `fix:`, `chore:`, `refactor:`
- 禁止自動 commit，只在明確要求時才執行
- 禁止提交 `.env` 或任何含 token 的檔案

## 回覆行為

- 修改程式碼時直接用工具改檔案，不要在對話裡貼出整份檔案內容或整個函式重複一遍
- 解釋修改內容時只講「為什麼改」和「改了什麼行為」，不要逐行解釋 Python/FastAPI
  的基礎語法
- 如果一個改動牽涉的設計取捨不只一種合理做法，才需要多花篇幅比較選項；
  否則直接照 Growth Discipline 的規則做，不用每次都詢問「要不要這樣改」

## DO NOT

- 不使用 `import *`
- 不使用可變預設引數
- 不用裸 `except:`
- 不用 `print()` 除錯，改用 `logging` module
- 不在 router 層做業務邏輯，只做分派