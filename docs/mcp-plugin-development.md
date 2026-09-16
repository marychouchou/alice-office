# MCP / Plugin 開發

這份文件是 README「[C. Plugin / MCP](../README.md#c-plugin--mcp)」搬出來的操作細節：
怎麼寫一個 Python MCP server、密鑰放哪、幾種測試 level、預裝的 `local-tools` plugin
怎麼運作。新程式碼該放 MCP 還是 plugin、docker SDK 只能在 `container_manager.py`
import 這類高層規則見 `AGENTS.md`「新程式碼放哪裡（路由表）」；三個 Python 環境
（`/opt/hermes/.venv`／`/opt/skills/.venv`／`/opt/tools/.venv`）分工的完整理由見
`docs/runtime-env-summary.md`。

## MCP server

MCP server 原始碼放在 `src/hermes/mcp/<name>/`（目前只有 `secretary/`）。**每個房間
第一次建立 container 時，會各自從這裡 seed 一份自己的、可自由編輯的副本**到
`data/<room_id>/mcp/<name>/`（見 `room_seed.py` 的 `ensure_mcp_seed`）——
之後房間之間互不影響，改一個房間的副本不會動到其他房間。這是 stdio MCP（Hermes
gateway 直接 spawn `node server.mjs` 子進程），每個房間各自一份 process，靠
`SECRETARY_LINE_USER_ID` = `room_id`（見 `src/hermes/mcp/secretary/mcp.manifest.yaml`）
做房間隔離。`src/hermes/mcp/` 底下有幾個子目錄，`ensure_mcp_seed` 就會幫每個新房間
各 seed 一份，`_format_mcp_section` 對每個房間 seed 出來的 MCP 各自產生一段
`mcp_servers.<name>` 寫進 `config.yaml`。

> 如果某個 MCP 天生就該所有房間共用同一份、不需要各房間各自客製化（例如純無狀態的
> 公用查詢服務），做成獨立的 HTTP/SSE sibling container、`config.yaml` 用
> `http://<container-name>:<port>` 連線，仍然是更省資源的選項——這裡的 seed 機制
> 是特別為了「每個房間需要能各自修改」這個需求設計的，不是唯一路徑。

**write-once（frozen）**：seed 只在房間第一次建立時發生一次，之後永不覆蓋——跟
`config.yaml` 的規則一樣，讓你放心手改房間自己的副本而不怕被蓋掉。代價是：改
`src/hermes/mcp/<name>/` 的原始碼**只會影響之後新建立的房間**，已存在的房間要嘛
自己去改它自己 `data/<room_id>/mcp/<name>/` 底下的那份，要嘛整個重建（見下方
「測試 MCP 修改」）。

> **開發時想把樣板一次推到所有已存在房間**（不逐房手改、也不整個重建）：
> `uv run python scripts/dev_sync_src.py`——監看 `src/hermes/{mcp,plugin}/` 與
> `config.template.yml`，變動時把樣板**強制覆蓋**每個房間的副本（含 config.yaml，
> 只保留房間各自的 `mcp/<name>/.env`）再 restart 所有 running 容器。dev 專用、
> **production 勿用**。跟 `watch_restart.py`（監看**單一房間自己的副本**）分工相反，
> 兩者服務不同開發流，見腳本 docstring。

MCP server 是 ESM（`"type": "module"`），依賴解析靠從檔案位置往上找 `node_modules`
（ESM 不吃 `NODE_PATH`）。每個房間的副本落在 `/opt/data/mcp/<name>/`（被房間自己的
bind mount 蓋住），所以共用的相依套件改烤在再上一層的 `/opt/node_modules`（見
`Dockerfile.hermes`）——所有房間、所有 MCP 共用同一份，改依賴版本要重 build image；
改 MCP 的程式邏輯只要房間自己 restart。

依賴清單是宣告式＋鎖版的：`src/hermes/mcp/package.json`（所有 MCP template 依賴的
聯集）+ 對應的 `src/hermes/mcp/package-lock.json`（`npm install --package-lock-only`
產生，commit 進版控），image build 時用 `npm ci` 安裝，可重現。
`tests/test_hermes_shared_node_deps.py` 會檢查每個 `src/hermes/mcp/<name>/package.json`
的每個 dependency 都以相同版本字串出現在共用的 `package.json`，避免漏同步。

### 如果要寫 Python MCP server

`/opt/tools/.venv`（`src/hermes/runtime/pyproject.toml` 管理的那個共用 venv）是給
**plugin script／skill 臨時用**的，**不是**給 Python MCP server 用的共用環境。每個
Python MCP 應該有自己專屬的 venv（自己的 `pyproject.toml` + `uv.lock`，image build
時 sync 進自己的路徑，例如 `/opt/mcp-venvs/<name>/.venv`），`mcp.manifest.yaml` 的
`command:` 直接指向該 venv 的直譯器絕對路徑（`/opt/mcp-venvs/<name>/.venv/bin/python3`），
不要指向共用的 `tools-python`——這樣不同 MCP 之間的套件版本才不會互相牽制，跟現在
每個 Node MCP 各自宣告 `package.json` 是同一個精神（Python 沒有 ESM walk-up那種可以
安全共用的機制，沒必要硬共用）。

另外要注意（[官方 MCP 文件](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)：
「For stdio servers, Hermes does not blindly pass your full shell environment.
Only explicitly configured `env` plus a safe baseline are passed through.」，
且在活的房間容器內 `docker exec` 讀真正在跑的 secretary MCP process 的
`/proc/<pid>/environ` 也實測驗證過）：Hermes gateway spawn MCP subprocess 時，
**預設只會繼承一小組安全基底環境變數**（實測為 `HOME`／`PATH` 這幾個），不是繼承
整個環境。`command:` 能找到執行檔是因為 `PATH` 有繼承（`/opt/node_modules/.bin`、
`/usr/local/bin` 都在裡面），但除此之外任何這個 MCP 需要的環境變數（API key、room
id 等）都要自己在 `mcp.manifest.yaml` 的 `env:` 區塊明確宣告，就像 `secretary`
宣告 `SECRETARY_LINE_USER_ID` 那樣——不能假設會從容器繼承到。`env:` 的值支援
`${VAR}` 內插語法，於 server 連線當下從環境變數（含 `~/.hermes/.env`）解析。

### 每個 MCP 自己的密鑰

`GOOGLE_MAPS_API_KEY` 這類 secretary MCP 專屬密鑰**不走這個 repo 的 `.env` /
router `Settings`**：房間第一次建立時，`ensure_mcp_seed` 會把
`src/hermes/mcp/secretary/.env.example` 複製成該房間自己的
`data/<room_id>/mcp/secretary/.env`——`server.mjs` 啟動時用 Node 內建的
`process.loadEnvFile()` 自己讀。之後要改哪個房間的密鑰，直接編輯那個房間自己的
`.env` 檔（`docker restart` 生效），不影響其他房間，也不用改這個 repo 任何地方。
router 完全不會碰到這個檔案的內容。

`.dockerignore` 排除了所有層級的 `.env`（`**/.env`），所以就算 `src/hermes/mcp/`
底下某個開發者的本機 checkout 不小心留了真的 `.env`，也不會被 `Dockerfile.hermes`
烤進 image、不會意外流入 image layer。

### 測試 MCP 修改

**Level 0（最快，不碰 Docker/Hermes）**——用官方 MCP inspector 直接對某個 MCP 樣板
打 stdio protocol：

```bash
cd src/hermes/mcp/secretary && npm install   # 第一次要裝依賴（僅供本機獨立測試用）
SECRETARY_LINE_USER_ID=test_room npx @modelcontextprotocol/inspector node server.mjs
```

開瀏覽器 UI，可直接呼叫個別 tool、驗 schema、看回傳值，不用經過 Hermes。

**Level 1（透過 Hermes 容器驗證，改房間自己的副本）**：

1. 確保測試房間容器已存在過一次（`ensure_mcp_seed` 才會把 MCP 樣板 seed 進
   `data/<room_id>/mcp/<name>/`）：`uv run python scripts/test_webhook.py --user-id U_LOCAL_TEST`
2. 直接改該房間自己的副本，例如 `data/U_LOCAL_TEST/mcp/secretary/tools/todo.mjs`——
   **不要改 `src/hermes/mcp/` 底下的樣板**，那份只在房間第一次建立時生效一次
3. `docker restart hermes_<room_id>` 讓 Hermes gateway 重新 spawn MCP server process，
   讀到新程式碼——這步可以用 `uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST`
   自動化，存檔即觸發
4. `uv run python scripts/test_webhook.py` 送會觸發該 tool 的訊息（例如「幫我加一筆待辦」），
   `docker logs hermes_<room_id>` 找 `[secretary-mcp] ready; lineUserId=...` 確認 spawn 成功、有無報錯

**要測「改了 repo 樣板之後全新房間長什麼樣」**：因為 write-once，既有測試房間看不到
樣板改動——用一個新的 `--user-id`，或 `docker rm -f hermes_<room_id>` 並刪掉
`data/<room_id>/{mcp,plugins,config.yaml}` 讓它下次重新從樣板 seed。

**Level 2（完整驗證，套件有變動時必跑）**：改了某個 MCP 的 `package.json`
（新增/升級依賴）時，因為共用的 `/opt/node_modules` 只在 image build 時安裝一次，
流程是：

1. 同步更新 `src/hermes/mcp/package.json`（所有 MCP 共用依賴的聯集）
2. 重新產生 lockfile：`cd src/hermes/mcp && npm install --package-lock-only`
3. 重 build image、bump `HERMES_IMAGE`、重建房間容器：

```bash
docker build -f Dockerfile.hermes -t alice-hermes-agent:v2 .
# .env 改 HERMES_IMAGE=alice-hermes-agent:v2，docker rm -f 測試房間容器重建
```

## 預裝 Plugin（local-tools）

`src/hermes/plugin/local-tools/` 是一套 Hermes standalone plugin（台灣薪資計算、法規查詢、工程計算機、長期記憶、AI 生態系索引、OCR、瀏覽器自動化），**每個房間第一次建立 container 時自動 seed 為預設工具**。運作方式：

- **原始碼**：房間第一次建立時，從 `src/hermes/plugin/local-tools/` seed 一份到該房間自己的
  `data/<room_id>/plugins/local-tools/`（見 `room_seed.py` 的 `ensure_plugin_seed`）——
  跟 MCP 一樣是 write-once：之後改 repo 樣板不會反映到已存在的房間，房間可以自由編輯
  自己的副本
- **啟用**：每個新房間的 `config.yaml` 模板自動寫入 `plugins.enabled: [local-tools]`
- **執行資料**（SQLite、快取）：落在各房間的 `/opt/data/local-tools-data/`（房間隔離，
  跟原始碼所在的 `/opt/data/plugins/local-tools/` 不同層）

工具的 Python 依賴分為兩類：

| 工具 | 依賴 | 上游 image 是否內建 |
|------|------|---------------------|
| hr / law / longmem / research | 純 stdlib | ✅ 直接可用 |
| math | `sympy` | ❌ 需衍生 image |
| image_ocr | `pymupdf` + 外部 Vision API | ❌ 需衍生 image + API server |
| webdriver | `selenium` + geckodriver + Firefox | ❌ 需衍生 image（plugin 自動隱藏） |

**Production 建法**——用 `Dockerfile.hermes` 建衍生 image 預裝 sympy + pymupdf +
selenium（烤進獨立的 `/opt/tools/.venv`，跟 plugin 原始碼本身無關——原始碼一律是
seed，從不烤進 image）：

```bash
docker build -f Dockerfile.hermes -t alice-hermes-agent:v1 .
# .env 設 HERMES_IMAGE=alice-hermes-agent:v1
```

> 一般開發時用上游 `nousresearch/hermes-agent` 即可，4 個 stdlib 工具直接可用。

只有當功能必須跑在 Hermes **進程內**（真 plugin，不是 MCP）才走衍生 image：
`FROM nousresearch/hermes-agent:<pin>`，改 `HERMES_IMAGE` 逐房重建。
這條路每次升級 Hermes 都要 rebase，成本高，沒必要不要走。

**Python 依賴是宣告式＋鎖版的**：`src/hermes/runtime/pyproject.toml`（third-party
套件清單）+ 對應的 `src/hermes/runtime/uv.lock`。跟 hermes-agent 自己的 venv
（`/opt/hermes/.venv`，只放 `tools.py` 這個 in-process plugin 層需要的 `pyyaml`）
完全隔離，不會被上游 Hermes base image 升級影響。加新依賴的流程：

1. 編輯 `src/hermes/runtime/pyproject.toml`
2. `cd src/hermes/runtime && uv lock` 重新產生 `uv.lock`，兩個檔都 commit
3. 重 build image、bump `HERMES_IMAGE`、重建房間容器（同上 MCP 依賴的三步驟）

容器內對應的執行環境是 `/opt/tools/.venv`：plugin 腳本用 `TOOLS_PYTHON` 環境變數解析
到這個 venv；login shell（`/etc/profile.d/90-alice-tools.sh`）也會 export 同一個
變數，並把 `/opt/node_modules/.bin` 加進 PATH，`/usr/local/bin/tools-python` 是
指向這個 venv 直譯器的 wrapper script，可在容器內任何 shell 直接呼叫。

**這個 venv 只給我們自己寫的東西用。** Hermes 官方 bundled skill 跑在另一個獨立的
`/opt/skills/.venv`（terminal 裡的 `python`／`pip` 就是它，官方 skill 文件照原文能跑），
預裝清單在 `src/hermes/runtime/skills-requirements.txt`，其餘由 agent runtime
`pip install`（容器本地）。三個環境的分工見 `AGENTS.md`「Hermes Container Model」。

### 測試 plugins 修改

**Level 0（最快，不碰 Docker/Hermes）**——每個 tool 是一支獨立可執行的 CLI script
（`tools.py` 用 `subprocess.run([PYTHON, script, *argv])` 呼叫，吃 CLI args、吐 JSON stdout），
可以直接跑，邏輯對不對這層就測得完：

```bash
python3 src/hermes/plugin/local-tools/scripts/hr/alice-payroll-engine.py --help
python3 src/hermes/plugin/local-tools/scripts/hr/alice-payroll-engine.py <實際參數>
```

**Level 1（驗證 Hermes 真的呼叫得到 tool，改房間自己的副本）**：

1. 確保測試房間容器已存在過一次（`ensure_plugin_seed` 才會把 plugin 樣板 seed 進
   `data/<room_id>/plugins/local-tools/`）
2. 直接改該房間自己的副本，例如 `data/U_LOCAL_TEST/plugins/local-tools/tools.py`——
   **不要改 `src/hermes/plugin/` 底下的樣板**，那份只在房間第一次建立時生效一次
3. `docker restart hermes_<room_id>`——新加的 tool 或改了 `plugin.yaml` / `schemas.py`
   需要 restart 才生效；純改 script 內容其實每次呼叫都是重新 spawn subprocess，
   通常不用重啟，但 restart 保險
4. `uv run python scripts/test_webhook.py` 送一句會觸發該 tool 的訊息，看 agent 回覆
5. 有問題就 `docker logs -f hermes_<room_id>` 看 stderr

**不想每次手動打 restart？** `scripts/watch_restart.py` 會輪詢指定房間自己 seed 出來的
`data/<room_id>/{mcp,plugins}/` 檔案異動，存檔自動 `docker restart hermes_<room_id>`：

```bash
uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST
```

只是把「你自己打 restart」自動化，容器怎麼建立、seed 什麼都還是
`container_manager.py` 那唯一一份邏輯決定的——不是另外養一份 compose service
設定，不會有兩份設定漂移的風險。

只有新增的 tool 需要新的 Python 套件（不在上游 image 也不在 `Dockerfile.hermes` 已裝清單裡）
時，才需要重 build 衍生 image——單純改 script 邏輯完全不用。
