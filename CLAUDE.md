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

## Commands

- `uv sync` — 安裝依賴
- `uv run fastapi dev` — 啟動開發伺服器（localhost:8000）
- `uv run pytest` — 執行所有測試
- `uv run pytest tests/test_foo.py::test_bar -v` — 執行單一測試
- `uv run pytest --cov=src --cov-report=term-missing` — 含覆蓋率
- `uv run mypy src/` — 型別檢查
- `uv run ruff check .` — lint 檢查
- `uv run ruff format .` — 格式化

提交前必跑：`uv run ruff check . && uv run mypy src/ && uv run pytest`

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