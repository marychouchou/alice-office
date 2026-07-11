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

## DO NOT

- 不使用 `import *`
- 不使用可變預設引數
- 不用裸 `except:`
- 不用 `print()` 除錯，改用 `logging` module
- 不在 router 層做業務邏輯，只做分派