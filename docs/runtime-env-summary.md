# 多語言工具執行環境（tools venv + 共用 node_modules）實作總結

2026-07-11 完成。解決的問題：各 MCP／skills 可能需要不同執行環境（Node、Python）
與各自的第三方套件，原本 Python 依賴硬寫在 `Dockerfile.hermes` 且直接裝進
hermes-agent 自己的 venv，Node 依賴沒有 lockfile、同步靠人工。

## 最終設計

| | Python | Node |
|---|---|---|
| 位置 | `/opt/tools/.venv`（獨立 venv，與 hermes-agent 的 `/opt/hermes/.venv` 隔離） | `/opt/node_modules`（ESM walk-up 解析） |
| 清單 | `src/hermes/runtime/pyproject.toml` | `src/hermes/mcp/package.json` |
| Lockfile | `src/hermes/runtime/uv.lock` | `src/hermes/mcp/package-lock.json` |
| 安裝方式 | build 時 `uv sync --locked --no-dev` | build 時 `npm ci --omit=dev` |
| 使用方式 | plugin 進程：`$TOOLS_PYTHON`；skill 終端：`tools-python` 指令 | `node`／bare import；CLI bins 在 PATH（`/opt/node_modules/.bin`） |

例外：`pyyaml` 仍裝在 `/opt/hermes/.venv` —— `tools.py`（plugin 層）是 in-process
跑在 Hermes agent 裡的，只有它需要。

官方 hermes-agent 的 skills **沒有**依賴宣告機制（SKILL.md 只是指令文件，靠 terminal
tool 執行），MCP 支援任意 stdio `command`，所以「全域環境 + 清單管理」正是官方生態
預期的做法。Runtime lazy install 上游已預設關閉（`HERMES_DISABLE_LAZY_INSTALLS=1`），
維持不動。

## 改動檔案

新增：

- `src/hermes/runtime/pyproject.toml` + `uv.lock` — tools venv 依賴清單（sympy、pymupdf、selenium）
- `src/hermes/runtime/profile-tools.sh` → image 內 `/etc/profile.d/90-alice-tools.sh`
  （login shell 會重設 PATH，Dockerfile `ENV` 到不了 skill 的 terminal session，需要這層）
- `src/hermes/mcp/package-lock.json` — Node 依賴 lockfile
- `src/hermes/skill/alice/runtime-env/SKILL.md`（+ 群組 `DESCRIPTION.md`）— 烤進
  `/opt/hermes/skills/alice/`，Hermes 開機 manifest sync 自動發到每個房間（含既有房間），
  告訴 agent「有哪些套件、用 `tools-python` 不要用裸 `python3`、怎麼加新套件」
- `tests/test_hermes_shared_node_deps.py` — 防呆：每個 MCP 的 package.json 依賴
  必須以相同 specifier 出現在共用 package.json（取代原本的人工同步註解）

修改：

- `Dockerfile.hermes` — hermes venv 只留 pyyaml；新增 /opt/tools venv 段落與
  `tools-python` wrapper；npm 改 `npm ci`；COPY skill；smoke test 擴充
- `src/hermes/plugin/local-tools/tools.py` — `PYTHON` 由 `TOOLS_PYTHON` 環境變數解析，
  找不到時 fallback 到 `sys.executable`（保住 host 端 Level-0 測試與舊 image）
- `README.md`、`CLAUDE.md` — 依賴工作流程與 container model 說明

## 過程中抓到並修掉的 bug

`tools-python` 原本做成 symlink 指向 venv 的 python —— CPython 會把 argv[0] 的
symlink 完全 resolve 到 base interpreter，跳過 `pyvenv.cfg` 偵測，`sys.prefix`
變成 `/usr`、找不到任何套件。改成 wrapper script（`exec /opt/tools/.venv/bin/python3 "$@"`）
後正常；build 的 smoke test 也改用 `tools-python` 呼叫並 assert `sys.prefix`，
以後這類問題 build 階段就會擋下。

## 驗證結果（全數通過）

- `uv run ruff check . && uv run mypy src/ && uv run pytest` — 89 passed
- `docker build -f Dockerfile.hermes -t alice-hermes-agent:v2 .` — build 內 smoke test 通過
- Image smoke（login shell）：`tools-python` → `sys.prefix=/opt/tools/.venv`、
  `import sympy, fitz, selenium` OK、PATH 含 `/opt/node_modules/.bin`
- E2E（compose router 以 `HERMES_IMAGE=alice-hermes-agent:v2` 重建，
  `scripts/test_webhook.py --user-id U_RUNTIME_TEST` 要求呼叫 math 工具）：
  - 房間容器建立、`skills/alice/runtime-env` 自動 sync 進房間
  - seed 的 `tools.py` 為新版（`TOOLS_PYTHON` 解析）
  - agent log：`tool math completed (0.93s, 196 chars)` — sympy 在 tools venv 下執行成功
  - secretary MCP（Node）註冊 18 個工具 — `npm ci` 的 `/opt/node_modules` 解析正常
  - 容器內實測：`docker exec ... sh -lc 'tools-python -c "import sympy"'` OK、
    `uv pip list --python $TOOLS_PYTHON` 列出 sympy 1.14.0／pymupdf 1.28.0／selenium 4.45.0
  - LINE push 回 400 為預期（假的 test user id）

E2E 測試容器已移除；`data/U_RUNTIME_TEST/` 保留（logs/ 內有上述證據），不需要可直接刪。

## 設計覆核（環境邊界三分法）

實作完成後跟使用者覆核了一輪環境邊界，確認的心智模型是三層：
`/opt/hermes/.venv`（hermes-agent 自己的，不裝任何我方套件）／`/opt/tools/.venv`
（我方 plugin/skill 共用）／`/opt/node_modules`（我方 Node MCP 共用）；原則是
「我方寫的 tool 一律用我方準備的環境，不用 hermes 自己的」。

覆核時額外查證兩點，並補進文件（`src/hermes/skill/alice/runtime-env/SKILL.md` +
本 README 的「如果要寫 Python MCP server」小節）：

- **local-tools plugin 的 subprocess scripts 是否需要 hermes 自己的 venv**：
  `grep` 全部 script 的 import，確認 math／OCR／law／memory／hr／browser 只 import
  標準庫 + sympy/selenium，**零 hermes-agent 內部套件依賴**——技術上沒有耦合需求，
  維持獨立 `/opt/tools/.venv` 的判斷成立，不需改回 hermes venv。
- **官方 MCP Python SDK 的 subprocess env 繼承規則**：讀了
  `/opt/hermes/.venv/lib/python3.13/site-packages/mcp/client/stdio/__init__.py`
  的 `get_default_environment()`，並在活的房間容器內 `docker exec` 讀真正在跑的
  secretary MCP node process 的 `/proc/<pid>/environ` 驗證：MCP subprocess 預設
  只繼承 `HOME/LOGNAME/PATH/SHELL/TERM/USER` 這組白名單，其他環境變數
  （含 `TOOLS_PYTHON`）都不會自動傳入，必須在該 MCP 自己的 `mcp.manifest.yaml`
  `env:` 區塊明確宣告——`PATH` 有被繼承（含 `/opt/node_modules/.bin`、
  `/usr/local/bin`），所以 `command: node` 解析不受影響，現有 secretary MCP 的做法
  本來就正確。

結論：未來若新增 Python MCP server，應各自建立專屬 venv（不跟 `/opt/tools/.venv`
共用，`command:` 指向該 venv 直譯器絕對路徑），目前 repo 尚無 Python MCP，此為
慣例文件補充，不涉及既有程式碼變更。

**後續修正**：使用者指出 MCP 的執行環境／env 本質上是 `config.yaml`
（`mcp_servers.<name>`）的設定範疇，不該寫成給 agent 讀的 skill 內容——agent 在
容器內跑，本來就沒有能力重build image／新增 MCP。查證
[官方 MCP 文件](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
確認 schema（`command`／`args`／`env`／`timeout`／`connect_timeout`／
`idle_timeout_seconds`／`max_lifetime_seconds`／`enabled`／
`supports_parallel_tool_calls`／`tools`）與現有 `secretary/mcp.manifest.yaml`
用法一致，且官方文件明確寫「Hermes does not blindly pass your full shell
environment. Only explicitly configured `env` plus a safe baseline are passed
through」，`env:` 值支援 `${VAR}` 內插（從含 `~/.hermes/.env` 的環境解析）。
因此把 `SKILL.md` 裡「Writing a new Python MCP server」整節移除（agent 用不上），
只保留 README「如果要寫 Python MCP server」這個開發者向小節，並補上官方文件連結。

## 之後怎麼加依賴

- **Python**：改 `src/hermes/runtime/pyproject.toml` → `cd src/hermes/runtime && uv lock`
  → rebuild image → bump `HERMES_IMAGE` → 重建房間容器
- **Node**：改 `src/hermes/mcp/package.json`（MCP 自己的 package.json 也要加，測試會擋）
  → `cd src/hermes/mcp && npm install --package-lock-only` → rebuild → 同上
- 容器內臨時實驗可 `uv pip install --python $TOOLS_PYTHON <pkg>`，但容器重建即消失，
  長久要走上面的 manifest 路徑

## 注意事項

1. **`.env` 還沒切**：目前 `.env` 的 `HERMES_IMAGE=nousresearch/hermes-agent`（上游素
   image，沒有這些依賴）。正在跑的 compose router 是我用環境變數覆蓋成
   `alice-hermes-agent:v2` 重建的；要固定下來請把 `.env` 改成
   `HERMES_IMAGE=alice-hermes-agent:v2`（否則下次 `docker compose up -d` 會蓋回上游 image）。
2. **既有測試房間**（U_LOCAL_TEST、U_TIMEOUT_TEST）：已依 clean-cut 決定刪除其
   `plugins/`、`config.yaml`（它們沒有 `mcp/`），下次收到訊息會用新樣板重 seed。
3. 尚未 git commit（依專案規範，等明確指示）。
