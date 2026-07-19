## Repo purpose

此專案讓每一個使用者可以透過 LINE（台灣常用的通訊軟體）access 他自己的 AI agent 企業助理。
AI agent 以 hermes agent 當作框架，每一個 chat room 即配置一個 AI agent；chat room 同時是
隔離邊界，chat room A 完全沒辦法 access chat room B。我們希望使用者在使用 LINE 時，完全
就像在使用自己部署的 hermes AI agent。LINE 是主要 channel；core 本身 channel-free，另有
first-party API channel 供 TUI／mobile／開發測試使用。

## 參考文件

- LINE Messaging API：https://developers.line.biz/en/docs/messaging-api/
- Hermes Agent：https://hermes-agent.nousresearch.com/docs

寫 LINE Messaging API 或 Hermes 相關程式碼前，先查上面的官方文件（用 Context7 或
WebFetch），不要憑記憶寫 API 呼叫——這兩個都是會改版的外部依賴。

## 文件地圖（docs/）

動手前先查這張表有沒有現成的說明，不要重新推導。另外 `.claude/rules/` 底下有按檔案
路徑觸發的 anti-pattern 登記，改到對應檔案時會自動載入，不用主動去讀。

| 什麼時候讀 | 讀這份 |
|---|---|
| 要看全系統架構圖（C4 三層） | `docs/architecture-c4.md` |
| 要理解一則訊息從 LINE 到 agent 再回來的完整路徑（`channels/` 逐檔導讀） | `docs/channels-walkthrough.md` |
| 追訊息解析、去重、批次的實作細節 | `docs/line-hermes-message-flow.md` |
| 改 router ↔ 容器內 agent 的 HTTP 協定前 | `docs/router-hermes-agent-protocol.md` |
| 搞不清 `DATA_DIR`／`HOST_DATA_DIR`／`HERMES_TEMPLATES_DIR` 誰是誰 | `docs/env-data-paths.md` |
| 動 tools venv 或共用 node_modules 前 | `docs/runtime-env-summary.md` |
| 不開手機 LINE 要測 end-to-end 時 | `docs/testing-paths.md` |
| 服務跑得起來但行為不對、要追 log／容器內 debug 時 | `docs/troubleshooting.md` |

歷史決策紀錄（想知道「為什麼當初這樣做」才讀）：`channel-interface-design.md`、
`channel-interface-plan.md`、`mcp-plugin-per-room-migration.md`、
`google-workspace-integration-summary.md`、`hermes-agent-line-gateway-comparison.md`。

新增 `docs/*.md` 時要同步加進這張表，不然等於沒寫。

## Stack

- **Language**: Python 3.12
- **Framework**: FastAPI
- **Package Manager**: uv（lockfile: `uv.lock`）
- **Linting**: Ruff（linter + formatter）、mypy（strict mode）
- **Testing**: pytest、pytest-asyncio

## Architecture

LINE Official Account (line OA) webhook router。
接收 LINE Platform 的 request，由對應的 channel adapter（`channels/line/adapter.py` 的
`LineAdapter`）解析 wire format、依事件類型（message、follow、postback 等）分派，交給
channel-free 的 `core.process_inbound`（gate → 容器 → agent），再由 adapter 送回房間。
完整三層圖見 `docs/architecture-c4.md`。

LINE Platform → POST /webhooks/line（LineAdapter）→ core.process_inbound → Container A／B／C（每房間一個）

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

實作過程中不用一直跑 lint／format；commit 前必跑：
`uv run ruff check . && uv run mypy src/ && uv run pytest`

## Coding Conventions

- 不使用 `Any` 型別，除非絕對必要（mypy strict 管不到 explicit `Any`，這條靠人守；
  現有邊界處的少數使用是天花板，不要再擴散）
- 其他機械可查的慣例——型別提示、`from __future__ import annotations`、pathlib、
  f-string、Google style docstring、函式長度、禁 `print`、裸 `except:`、可變預設
  引數、`import *`——全部由 ruff／mypy 強制，以 `pyproject.toml` 為準，不在這裡重複

## Growth Discipline

codebase 變大時的結構規則。每一條都是「訊號 → 動作」，看到訊號就做，不需要憑感覺判斷「夠不夠乾淨」。

### 門檻訊號

- **Rule of Three**：相同邏輯第 2 次出現 → 允許複製，但在兩處都留註解指向彼此；
  第 3 次出現 → 抽成共用函式/模組。第 1 次出現時不要預先抽象。
- **對同一個值的 if/elif 達 4 個分支**（如 LINE `msg_type`、MCP tool `name`、plugin
  `command`）→ 改成 dict dispatch table（`{key: handler}`）。3 個以下維持 if/elif。
  repo 內做對的例子：`channels/line/events.py` 的 `_MESSAGE_HANDLERS` 對 `msg_type` 用
  dispatch table（2026-07-12 從 router 的 4 分支 if/elif 改過來）。
- **單一檔案超過 400 行** → 檢查是否混了兩種以上「改動理由」（用下方路由表的分類判斷）。
  有混 → 依改動理由拆檔；沒混（單一主題只是長，如 `google_oauth.py` 396 行整檔都是
  OAuth）→ 不動。
- **同一個 `data/<room_id>/` 子路徑在兩個以上模組各自拼字串** → 提升成 `config.py`
  Settings 的 method（比照 `room_google_dir` 的做法），不要留兩份拼法。
- **函式內控制結構巢狀超過 3 層**（如 if 裡 for 裡 try 裡 if；elif 鏈不算加一層）→
  用 early return／guard clause／抽子函式打平到 3 層以內。現況超標的函式已登記在
  `.claude/rules/` 對應檔案（按路徑觸發）。

### 消除特殊情況（寫完函式後的檢查點）

這不是要求現在重構既有程式碼，是**新增程式碼時**的檢查點：寫完一個函式後，數一下
裡面有幾個「這是特殊情況所以要分開處理」的分支（`if is_first_time`、
`if not exists then create else use`、`if xxx_enabled` 這類）。**超過一個**就先想：
能不能靠改資料結構或初始化方式讓正常路徑直接涵蓋它，而不是保留 if 繞過去。
特殊情況的數量反映的是資料結構／介面設計得好不好。

- repo 內做對的例子：`google_oauth._load_tokens` 對不存在的檔案直接回 `{}`，
  所以所有呼叫端都沒有「tokens.json 還沒建立」的分支。新的讀取類 helper 比照辦理
  （在邊界把缺失正規化掉，讓呼叫端只有一條路徑）。
- 邊界提醒：`_seed_templates` 的 `if dest_dir.exists(): continue` 是 write-once
  語意本身（見 Hermes Container Model），不是待消除的特殊情況——不要「修」它。

### 新程式碼放哪裡（路由表）

依「這段程式碼將來會因為什麼原因被改動」決定位置：

| 改動理由 | 放這裡 |
|---|---|
| LINE wire format（event 結構、訊息型別解析、簽章、送訊長度/則數限制） | `src/alice_office_router/channels/line/` |
| 新通訊通道（webhook 解析、驗簽、送訊） | `src/alice_office_router/channels/<name>/`，core 只認 `InboundMessage` |
| container 生命週期（建立/啟動/健康等待/URL 解析） | `container_manager.py`——**docker SDK 只允許在這個檔案 import** |
| 房間 write-once seed（複製 template 到 `data/<room_id>/`） | 目前在 `container_manager.py`；要新增 seed 種類時，先把 seed 函式群抽成 `room_seed.py` 再加 |
| 「這則訊息該不該進 agent」的 gate 判斷（如 Google OAuth gate） | 獨立模組提供回傳 status 的純函式（比照 `google_oauth.check_google_authorization`），router 只呼叫、不寫判斷內容 |
| 對 Hermes agent 的 HTTP 協定 | `hermes_client.py` |
| 環境變數與路徑推導 | `config.py` 的 Settings |
| 新的 agent 能力（工具） | `src/hermes/mcp/<name>/` 或 `src/hermes/plugin/` 的 template，不是 router 的功能 |

docker SDK 隔離用最小範例釘死：

```python
# ❌ 在 container_manager.py 以外 import docker
import docker

# ✅ 其他模組只 import container_manager 包好的函式
from alice_office_router.container_manager import get_or_create_container
```

### Known Anti-Patterns

已搬到 `.claude/rules/`（`container-manager.md`／`hermes-mcp.md`／`hermes-plugin.md`），
按 `paths:` 在改到對應檔案時自動載入。新增 anti-pattern 時寫進對應的 rules 檔，
不要加回這裡。

## Error Handling

- 禁止靜默吞掉例外：捕捉具體例外並記錄 log（`logger.error`）
- 使用 context manager（`with`）管理資源
- LINE webhook 驗簽失敗必須回傳 400，不可靜默忽略

## Testing

- 遵循 Arrange-Act-Assert 結構
- mock 所有外部依賴（LINE API、下游 container）
- 測試輸出資料夾加入 `.gitignore`

## Security

- LINE Channel Secret 與 Access Token 只存 `.env`
- 確保 `.env` 在 `.gitignore`
- 不要修改使用者的 `.env`；新增或改名環境變數時，同一個 commit 同步更新 `.env.example`
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

- 不在 router 層做業務邏輯，只做分派——gate 判斷住在獨立模組、回傳 status 的
  純函式（見路由表），router／core 只依結果分派
