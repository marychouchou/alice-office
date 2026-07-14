# alice-office-router

LINE OA 多租戶 Webhook 路由器。接收來自 LINE 平台的 Webhook，依據聊天室 ID 動態建立隔離的 Docker 容器（真實的 [Hermes Agent](https://github.com/NousResearch/hermes-agent)），把訊息轉發給對應容器的 LLM 大腦，再由 router 自己把回覆推播回 LINE。

## 架構概覽

Router 擁有 LINE 進出的全部責任（驗簽、收訊息、push 回覆）；Hermes container 完全不碰 LINE，只透過內建的 `api_server` platform（OpenAI-compatible API）被動回答問題。

```mermaid
graph TD
    LINE["LINE 平台"]

    subgraph Host["Docker Host"]
        Sock[("/var/run/docker.sock")]

        subgraph Net["hermes_global_net (Docker bridge network)"]
            Router["alice-office-router<br/>FastAPI · :8000"]
            HermesA["hermes_room_A<br/>nousresearch/hermes-agent<br/>gateway run · api_server :8642"]
            HermesB["hermes_room_B<br/>..."]
        end

        DataA[("./data/room_A<br/>→ /opt/data")]
        DataB[("./data/room_B<br/>→ /opt/data")]
    end

    LINE -- "POST /webhook<br/>(x-line-signature)" --> Router
    Router -- "docker.from_env()<br/>get_or_create_container" --> Sock
    Sock -.controls.-> HermesA
    Sock -.controls.-> HermesB
    Router <-- "POST /v1/chat/completions<br/>Bearer HERMES_API_SERVER_KEY" --> HermesA
    Router <-- "POST /v1/chat/completions" --> HermesB
    Router -- "Push Message API" --> LINE
    HermesA --- DataA
    HermesB --- DataB
```

每個聊天室擁有獨立容器與獨立資料夾，容器之間無法互相存取。Router 透過掛載的 `docker.sock` 控制這些「兄弟容器」（sibling containers）——這個模式讓 router 本身也能跑在 container 裡（見下方「部署模式」）。

單則訊息的完整流程：

```mermaid
sequenceDiagram
    autonumber
    actor User as LINE 使用者
    participant LINE as LINE 平台
    participant Router as alice-office-router
    participant Docker as Docker Engine
    participant Agent as "hermes_{room_id}"

    User->>LINE: 傳送訊息
    LINE->>Router: POST /webhook (x-line-signature)
    Router->>Router: 驗證簽章 + 解析 room_id / 文字
    Router-->>LINE: 200 OK（立即回應，避免逾時）

    Note over Router: 以下在背景任務中執行
    Router->>Docker: get_or_create_container(room_id)
    alt 容器不存在或已停止
        Docker->>Agent: docker run nousresearch/hermes-agent gateway run
        Router->>Agent: GET /health（輪詢直到 ready，最多 60 秒）
    end
    Router->>Agent: POST /v1/chat/completions<br/>(Bearer key, X-Hermes-Session-Id)
    Agent-->>Router: 回覆文字
    Router->>LINE: Push Message API
    LINE->>User: 顯示回覆
```

完整訊息流程（去重、背景任務、錯誤處理）見 `docs/line-hermes-message-flow.md`；router↔container 協定細節見 `docs/router-hermes-agent-protocol.md`；為何不用 Hermes 內建 LINE gateway、兩者能力對照見 `docs/hermes-agent-line-gateway-comparison.md`。

## LINE 訊息類型支援

Router 會處理整個 webhook body 裡的**所有** event（不只第一個），逐一解析、去重、排背景任務：

| 訊息類型 | 處理方式 |
|---|---|
| `text` | 直接轉發文字給 Hermes agent |
| `image` / `audio` / `video` / `file` | 用 LINE Content API 下載二進位內容，寫進該房間掛載的 volume（`data/<room_id>/incoming/`，container 內對應 `/opt/data/incoming/`），送一則文字通知 agent 檔案路徑——由 container 內**真正的 Hermes agent** 用自己的 vision/STT/檔案工具處理，router 不做任何內容解析 |
| `sticker` / `location` | 轉成佔位文字（如 `[使用者傳送了貼圖：...]`）送給 agent |
| 其他／未知類型 | 記錄一行 log 後略過 |

回覆時：

- **Reply token 優先、Push 為 fallback**：webhook 事件裡的 `replyToken`（免費、單次、~60 秒內有效）優先使用；若已過期或被 LINE 拒絕，自動 fallback 到 Push Message API。
- **長文自動分段 + Markdown 去除**：LLM 回覆會先去除 LINE 無法渲染的 Markdown 語法（保留連結可點擊），再依 LINE 單則 bubble 5000 字上限智慧分段（最多 5 則/次）。
- **Webhook 事件去重**：LINE 的 webhook 是 at-least-once 語意，可能重送同一個 event；router 用 `webhookEventId` 做 in-memory 去重，避免同一則訊息被回覆兩次。

以上邏輯 1:1 參考自 Hermes Agent 內建 LINE adapter 的演算法（詳見 `docs/hermes-agent-line-gateway-comparison.md`），但因為架構不同（router 與 container 分離、只透過 `api_server` + 共用 volume 溝通），媒體處理走的是「檔案落地 + 文字通知」而非 Hermes 內建的多模態 API 路徑。

Outbound 媒體（agent 主動產生圖片/語音/影片送回 LINE）與 slow-LLM postback 按鈕尚未實作，見同一份文件的
「未做（Phase 2）」項目。

## 部署模式

`ROUTER_IN_DOCKER` 決定 router 怎麼找到 Hermes 容器：

| 模式 | `ROUTER_IN_DOCKER` | Router 執行位置 | 如何連到 Hermes 容器 |
|---|---|---|---|
| 本機開發 | `false` | Host OS（`uv run uvicorn ...`） | 容器建立時發布隨機 host port，router 走 `http://localhost:<port>` |
| Container 化（正式/未來） | `true`（預設） | 自己也是 `hermes_global_net` 上的一個容器 | 直接用容器名稱解析，如 `http://hermes_room_A:8642` |

Container 化模式已經在 `docker-compose.yml` 中就緒——把 `/var/run/docker.sock` 掛進 router 自己的容器，讓它能對 Host 的 Docker Daemon 下指令生成「兄弟容器」（sibling containers），而不是需要 Docker-in-Docker：

```bash
docker compose up -d --build
```

已用 `docker compose up` + 真實的 LINE webhook 請求驗證過：router 在自己的 container 內仍能正常呼叫 `docker.sock` 建立 `hermes_{room_id}` 容器、透過容器名稱互連、並把回覆 push 回 LINE。

正式部署時 `.env` 用真的 LINE 憑證、`ROUTER_IN_DOCKER=true`，並把 LINE OA 的
Webhook URL 設為 `https://your-domain.com/webhook`（服務監聽 `http://localhost:8000`）。
日常開發不用起 compose——只在動到 Dockerfile / compose / `container_manager.py`
連線邏輯時，才需要用 container 模式驗一次。

## 環境需求

- Docker（宿主機）
- Python 3.12（本地開發用）
- [uv](https://docs.astral.sh/uv/)（套件管理）
- [ngrok](https://ngrok.com/)（選配——只有接真 LINE 端到端驗收時需要）

## 快速開始

從 git clone 到改 code 看到更動。照著做即可，全程不需要真的 LINE channel——
`scripts/test_webhook.py` 會模擬 LINE 平台的簽章與訊息。

### 1. 安裝依賴

```bash
uv sync
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`：

```env
LINE_CHANNEL_SECRET=dev-fake-secret        # 開發用假值即可，test_webhook.py 用它算簽章
LINE_CHANNEL_ACCESS_TOKEN=dev-fake-token   # 同上——接真 LINE 驗收才需要真憑證
ROUTER_IN_DOCKER=false                     # 開發用 host 模式；容器化部署才設 true
DATA_DIR=/absolute/path/to/alice-office-router/data       # host 模式下必填，見下方註解
HOST_DATA_DIR=/absolute/path/to/alice-office-router/data
HERMES_TEMPLATES_DIR=/absolute/path/to/alice-office-router/src/hermes  # host 模式下必填，見下方註解
HERMES_IMAGE=alice-hermes-agent:v1
HERMES_API_SERVER_KEY=change-me            # openssl rand -hex 32
LLM_BASE_URL=change-me                     # 唯一不能假的：可用的 OpenAI-compatible endpoint
LLM_API_KEY=change-me
LLM_MODEL=change-me
```

> `HOST_DATA_DIR` 必須是**宿主機**的絕對路徑，Docker 掛載 volume 時需要用到。
> **host 模式（`uv run fastapi dev`）下 `DATA_DIR`／`HERMES_TEMPLATES_DIR` 必須另外覆寫**
> 成跟 `HOST_DATA_DIR`／repo 的 `src/hermes` 一樣的絕對路徑，否則 router 會嘗試在宿主機上
> 建立不存在的預設路徑（`/app/data`、`/app/hermes-templates`）而建房間失敗；忘了覆寫時
> `Settings` 的 `model_validator` 會在 app 啟動當下直接 fail-fast，不用等到建房間才發現。
> 這幾個變數的關係與為什麼要分開，見 `docs/env-data-paths.md`。
> `HERMES_API_SERVER_KEY` 是 router 與每個 Hermes 容器共用的密鑰。
> `LLM_*` 是共用的 LLM 後端設定，會自動寫入每個新房間的 `config.yaml`。

### 3. 建立 Docker 網路、準備 Hermes image

```bash
docker network create hermes_global_net
docker build -f Dockerfile.hermes -t alice-hermes-agent:v1 .   # 含 plugin + MCP 共用依賴
```

> `hermes_global_net` 在 `docker-compose.yml` 中宣告為 `external`，沒先建立會直接啟動失敗。
>
> 趕時間可以跳過 build，先 `docker pull nousresearch/hermes-agent:<pinned-tag>`（Docker Hub
> 公開 image，免權限）填進 `HERMES_IMAGE`——local-tools 的 4 個 stdlib 工具能動，但
> math／OCR／webdriver 與 secretary-mcp（缺 `/opt/node_modules`）不行，差別見
> 「[預裝 Plugin](#預裝-pluginlocal-tools)」。
> 不論哪種，`HERMES_IMAGE` 都 **pin 版本 tag**，不要 `latest`——版本漂移是這個架構最容易踩的雷之一。

### 4. 啟動 router、建立測試房間

```bash
uv run fastapi dev src/alice_office_router/main.py        # terminal A，保持開著
uv run python scripts/test_webhook.py --user-id U_LOCAL_TEST --text "你好"   # terminal B
```

確認容器自動建立：

```bash
docker ps | grep hermes_U_LOCAL_TEST
ls data/U_LOCAL_TEST/          # 內含自動產生的 config.yaml
docker logs hermes_U_LOCAL_TEST | grep "/v1/chat/completions"  # Hermes agent 收到並回覆了訊息
```

第一次觸發某個房間時會拉起 `hermes_<room_id>` 容器，Hermes 開機（s6 supervision + skill sync）
需要 30–60 秒，不是卡住。成功後 `data/<room_id>/` 會出現完整的 agent home
（sessions、memories、skills…），其中 `mcp/` 與 `plugins/` 是從 `src/hermes/{mcp,plugin}/`
自動 seed 出來的**這個房間自己的副本**（見「[C. Plugin / MCP](#c-plugin--mcp)」）——這個
目錄就是該房間的「記憶」＋「工具原始碼」，容器可以隨時砍掉重建而不失憶，但除了
`mcp/`／`plugins/`（本來就是給你編輯的）之外，不要手動修改裡面的其他狀態檔。

agent 的回覆去哪看：假 user id 推不回真的 LINE（router log 出現 `Failed to push LINE reply`
屬預期），所以看 `docker logs -f hermes_U_LOCAL_TEST` 與 router terminal 的 log。

### 5. 日常開發迴圈

```bash
# terminal A：router
uv run fastapi dev src/alice_office_router/main.py

# terminal B：watcher——監看測試房間自己 seed 出來的 mcp/plugins 副本，存檔自動 restart
uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST

# terminal C：改 code → 存檔 → 等 watcher 顯示 restart 完成（warm restart 約 10–15 秒）
#            → 送訊息驗證
vim data/U_LOCAL_TEST/plugins/local-tools/tools.py    # 或 data/U_LOCAL_TEST/mcp/secretary/tools/*.mjs
uv run python scripts/test_webhook.py --user-id U_LOCAL_TEST --text "呼叫 math 工具，expression=\"2+2\""
```

- 改 **router code**（`src/alice_office_router/`）：`fastapi dev` 自己會 reload，不用動任何容器。
- 改 **plugin / MCP**：改的是**測試房間自己的副本**（`data/<room_id>/{plugins,mcp}/`，不是
  `src/hermes/` 底下的樣板——樣板只在房間第一次建立時 seed 一次），watcher 自動 restart
  測試房間。更細的生效條件見「[C. Plugin / MCP](#c-plugin--mcp)」。
- 改 **skill**：放進 `data/<room_id>/skills/` 後 restart 該房間（見「[B. Hermes skill](#b-hermes-skill)」）。

### 接真的 LINE（端到端驗收才需要）

每位開發者自建免費 LINE OA，互不干擾（一個 channel 同時只能設一個 webhook URL，共用會互搶）：

1. [LINE Developers Console](https://developers.line.biz/) → 建 Provider → 建 **Messaging API** channel，
   把 channel secret / access token 填入 `.env`
2. `ngrok http 8000`，將 `https://<id>.ngrok-free.app/webhook` 填入 channel 的 Webhook URL，
   開啟 "Use webhook"
3. 用手機加該 OA 為好友，傳訊息 → 應收到 Hermes 回覆

## 開發工作流程

### 三條開發線

改動前先認清你在改哪一種東西——三者的生效方式與交付路徑完全不同：

| 交付物 | 改動位置 | 生效方式 | 頻率 |
|--------|----------|----------|------|
| **A. Router feature** | `src/alice_office_router/` | 重建 router image | 低頻 |
| **B. Hermes skill** | skill 檔案（`SKILL.md` + `scripts/`） | 放進房間的 `/opt/data/skills/`，restart 容器 | 高頻 |
| **C. Plugin / MCP** | MCP server（獨立 HTTP/SSE 容器）或 Hermes 衍生 image | 改房間 `config.yaml` + restart；或換 `HERMES_IMAGE` | 低頻 |

共同的鐵律（對 Hermes `0.18.0` 實測確認）：

- `HERMES_HOME=/opt/data`——掛載的 volume 就是完整 agent home，
  **file-drop 設定（skills / plugins / hooks / MCP / SOUL.md）都真的有效**。
- **沒有熱載入**。透過 `/v1/chat/completions` 送 `/reload-mcp` 之類的 slash command
  不會被攔截（會被當一般文字丟給 LLM）。設定變更的通用生效手段就是 **restart 容器**。
- 部署版本的 Jobs REST API（`/api/jobs`）**沒有開**（`/v1/capabilities` 回報
  `jobs_admin: false`）——官方文件寫有不代表真的有，開發前先打 `/v1/capabilities` 確認。

### A. Router feature

Trunk-based，短分支：

```bash
git checkout -b feat/xxx        # 或 fix/xxx
# ... host 模式開發，scripts/test_webhook.py 隨手驗 ...
uv run ruff check . && uv run mypy src/ && uv run pytest   # 提交前必跑
```

- `main` 永遠保持可 release。做一半的功能用環境變數 feature flag 藏起來照樣合併——
  **小步合併，不養長分支**。
- 單元測試 mock 掉 LINE API 與 Docker（照 `tests/conftest.py` 現有模式）；
  但編排邏輯（`container_manager.py`）的改動要另外用 `scripts/test_webhook.py`
  對真容器驗一次——單元測試全 mock，測不到最容易壞的 Docker 層。

### B. Hermes skill

Skill 是純檔案，格式照 `data/<room>/skills/` 裡的現成範例
（`DESCRIPTION.md` + 各 skill 的 `SKILL.md`，選配 `scripts/`、`references/`）：

1. 把 skill 放進自己測試房間的 `data/<room_id>/skills/<name>/`
2. `docker restart hermes_<room_id>`
3. 用 `test_webhook.py` 送會觸發該 skill 的訊息驗證

### C. Plugin / MCP

MCP server 原始碼放在 `src/hermes/mcp/<name>/`（目前只有 `secretary/`）。**每個房間
第一次建立 container 時，會各自從這裡 seed 一份自己的、可自由編輯的副本**到
`data/<room_id>/mcp/<name>/`（見 `container_manager.py` 的 `_ensure_mcp_seed`）——
之後房間之間互不影響，改一個房間的副本不會動到其他房間。這是 stdio MCP（Hermes
gateway 直接 spawn `node server.mjs` 子進程），每個房間各自一份 process，靠
`SECRETARY_LINE_USER_ID` = `room_id`（見 `src/hermes/mcp/secretary/mcp.manifest.yaml`）
做房間隔離。`src/hermes/mcp/` 底下有幾個子目錄，`_ensure_mcp_seed` 就會幫每個新房間
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

#### 如果要寫 Python MCP server

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

#### 每個 MCP 自己的密鑰

`GOOGLE_MAPS_API_KEY` 這類 secretary MCP 專屬密鑰**不走這個 repo 的 `.env` /
router `Settings`**：房間第一次建立時，`_ensure_mcp_seed` 會把
`src/hermes/mcp/secretary/.env.example` 複製成該房間自己的
`data/<room_id>/mcp/secretary/.env`——`server.mjs` 啟動時用 Node 內建的
`process.loadEnvFile()` 自己讀。之後要改哪個房間的密鑰，直接編輯那個房間自己的
`.env` 檔（`docker restart` 生效），不影響其他房間，也不用改這個 repo 任何地方。
router 完全不會碰到這個檔案的內容。

`.dockerignore` 排除了所有層級的 `.env`（`**/.env`），所以就算 `src/hermes/mcp/`
底下某個開發者的本機 checkout 不小心留了真的 `.env`，也不會被 `Dockerfile.hermes`
烤進 image、不會意外流入 image layer。

#### 測試 MCP 修改

**Level 0（最快，不碰 Docker/Hermes）**——用官方 MCP inspector 直接對某個 MCP 樣板
打 stdio protocol：

```bash
cd src/hermes/mcp/secretary && npm install   # 第一次要裝依賴（僅供本機獨立測試用）
SECRETARY_LINE_USER_ID=test_room npx @modelcontextprotocol/inspector node server.mjs
```

開瀏覽器 UI，可直接呼叫個別 tool、驗 schema、看回傳值，不用經過 Hermes。

**Level 1（透過 Hermes 容器驗證，改房間自己的副本）**：

1. 確保測試房間容器已存在過一次（`_ensure_mcp_seed` 才會把 MCP 樣板 seed 進
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

#### 預裝 Plugin（local-tools）

`src/hermes/plugin/local-tools/` 是一套 Hermes standalone plugin（台灣薪資計算、法規查詢、工程計算機、長期記憶、AI 生態系索引、OCR、瀏覽器自動化），**每個房間第一次建立 container 時自動 seed 為預設工具**。運作方式：

- **原始碼**：房間第一次建立時，從 `src/hermes/plugin/local-tools/` seed 一份到該房間自己的
  `data/<room_id>/plugins/local-tools/`（見 `container_manager.py` 的 `_ensure_plugin_seed`）——
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

##### 測試 plugins 修改

**Level 0（最快，不碰 Docker/Hermes）**——每個 tool 是一支獨立可執行的 CLI script
（`tools.py` 用 `subprocess.run([PYTHON, script, *argv])` 呼叫，吃 CLI args、吐 JSON stdout），
可以直接跑，邏輯對不對這層就測得完：

```bash
python3 src/hermes/plugin/local-tools/scripts/hr/alice-payroll-engine.py --help
python3 src/hermes/plugin/local-tools/scripts/hr/alice-payroll-engine.py <實際參數>
```

**Level 1（驗證 Hermes 真的呼叫得到 tool，改房間自己的副本）**：

1. 確保測試房間容器已存在過一次（`_ensure_plugin_seed` 才會把 plugin 樣板 seed 進
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

### 驗證層級（由快到慢）

| 層級 | 工具 | 驗什麼 | 什麼時候跑 |
|------|------|--------|-----------|
| 1 | `pytest`（全 mock） | router 邏輯 | 每次改動，秒級 |
| 2 | `scripts/test_webhook.py` | 驗簽 → 容器編排 → LLM 整條路 | 動到編排/協定時 |
| 3 | 真 LINE（自建 OA + ngrok） | LINE 平台行為（媒體、reply token…） | 驗收、動到 LINE 相關 code 時 |
| 4 | canary 房間 | 正式環境、真使用者流量 | release 前 |

## Google Workspace 整合

Calendar / Gmail / Drive 三個 MCP，讓每個房間各自完成 Google OAuth 後，agent 可以讀寫
該房間使用者自己的日曆／信箱／雲端硬碟。跟 `secretary` 一樣是 per-room seed、per-room
process，token／憑證也逐房隔離（`data/<room_id>/google/`），房間之間互不可見；
`rm -rf data/<room_id>` 會把該房間的 Google 授權一併清空，需重新授權（見下方
「疑難排解」的完整重置流程）。

**完整架構決策**（為何 oauth gate 是 router 邏輯而非 Hermes plugin、為何原本獨立的
Flask OAuth server 併進了 router、憑證掛載路徑與 seed 時序的取捨）**見
`docs/google-workspace-integration-summary.md`**；下面只列出實際設定與操作步驟。

### GCP Console 設定摘要

1. 建立/選擇 GCP 專案 → 啟用三個 API：**Google Calendar API**、**Gmail API**、
   **Google Drive API**。
2. 設定 OAuth 同意畫面（Consent screen）。
3. 建立**兩個** OAuth 用戶端 ID（兩者用途不同，缺一不可）：
   - **Web application**：Authorized redirect URIs 加入
     `{GOOGLE_OAUTH_PUBLIC_URL}/oauth/callback`（LINE 使用者瀏覽器走的授權流程用，
     router 的 `/oauth/start` `/oauth/callback` 兩個路由靠它）。
   - **Desktop app（Installed）**：`@cocal/google-calendar-mcp` 跟
     `scripts/google_reauth.py` 用它識別身份，走 localhost redirect，不需要在
     GCP Console 額外設定 redirect URI。

### 檔案放置

兩份 credentials JSON 只需下載**一次**，放到部署層的種子來源（**不進版控**，`data/`
本身已在 `.gitignore`）：

```
data/_google/gcp-oauth.keys.json            ← Web application client（種子來源，只放一次）
data/_google/gcp-oauth.keys.installed.json  ← Desktop (Installed) client（種子來源，只放一次）
```

之後每個房間會在自己第一次接觸 Google OAuth 時（seed 時序細節見
`docs/google-workspace-integration-summary.md`），由
`container_manager.ensure_google_seed` 自動從這裡複製一份到
`data/<room_id>/google/`——**不需要、也不應該**手動幫每個房間各放一次：

```
data/<room_id>/google/gcp-oauth.keys.json            ← 這個房間自己的副本（write-once）
data/<room_id>/google/gcp-oauth.keys.installed.json  ← 這個房間自己的副本（write-once）
data/<room_id>/google/tokens.json                    ← 執行期自動產生，不用手動放
```

> **Linux host 部署注意**：container 內的 MCP process 以 `hermes`（uid 10000）
> 執行，且 token refresh 會**寫回** `tokens.json`，所以每個房間的
> `data/<room_id>/google/` 都必須讓 uid 10000 可讀＋可寫（例如
> `chown -R 10000 data/<room_id>/google` 或 `chmod 777 data/<room_id>/google`，
> 新房間建立時記得補跑）。macOS 的 Docker Desktop 透過檔案共享層自動處理權限對映，
> 不需要手動調。

### 環境變數

| 變數 | 說明 |
|------|------|
| `GOOGLE_OAUTH_PUBLIC_URL` | 這個 router 的公開 HTTPS base URL（不含結尾斜線）。留空（預設）＝整個 Google 整合停用：oauth 路由回 400、新房間不 seed 這三個 MCP、訊息也不會被攔。 |
| `GOOGLE_OAUTH_GATE` | 預設 `true`。設 `false` 時 oauth 路由照常運作，只是不擋任何房間的訊息（適合先把 MCP 跑起來、還沒想清楚要不要強制授權的階段）。 |

`Settings.google_oauth_enabled`（`config.py`）同時檢查
`GOOGLE_OAUTH_PUBLIC_URL` 非空**且** `data/_google/gcp-oauth.keys.json` 存在，兩者缺一都視為停用。

### 訊息授權判斷流程

`check_google_authorization` 每則訊息都會跑一次，`ok`／`notice`／`blocked` 三種結果對應不同行為：

```mermaid
flowchart TD
    Start(["收到訊息，準備呼叫 agent 前"]) --> Enabled{"google_oauth_enabled<br/>且 GOOGLE_OAUTH_GATE？"}
    Enabled -- "否" --> Ok1["ok：直接放行"]
    Enabled -- "是" --> HasToken{"這個房間自己的<br/>tokens.json 有 token？"}
    HasToken -- "沒有" --> Blocked["blocked：回授權連結<br/>不呼叫 agent"]
    HasToken -- "有" --> Expired{"access_token 過期？"}
    Expired -- "是且無 refresh_token" --> Blocked
    Expired -- "否，或有 refresh_token" --> Scopes{"scope 包含<br/>calendar/gmail.modify/drive？"}
    Scopes -- "缺 Drive scope" --> Notice["notice：推播重新授權提示<br/>仍呼叫 agent（calendar/gmail 可用）"]
    Scopes -- "齊全" --> Ok2["ok：正常呼叫 agent"]
```

### lowercase 帳號 key（容易忽略、務必注意）

`@cocal/google-calendar-mcp` 驗證 `GOOGLE_ACCOUNT_MODE` 必須符合
`/^[a-z0-9_-]{1,64}$/`（只准小寫），但 LINE room id 開頭是大寫 `U`/`C`/`R`。
因此整個 Google 整合統一用 **`room_id.lower()`** 當帳號 key（見
`alice_office_router.google_oauth.account_key`）：這個房間自己的 `tokens.json`
裡的 key、`/oauth/callback` 存 token、gate 檢查、三個 MCP manifest 的
`{account_key}` 佔位符，全部都是同一個 lowercase key，不能有任何一處漏掉轉換，
否則會出現「明明授權過但 gate 還是說沒授權」這種對不起來的情況。

**跟上面不同的另一件事：`room_id` 本身（原始大小寫）決定資料夾位置，絕對不能被
lowercase 污染。** `data/<room_id>/google/` 這個路徑用的是原始 `room_id`（跟
`data/<room_id>/mcp`、`plugins` 同一個變數），只有寫進 `tokens.json`**裡面**的
key 才轉小寫。`google_oauth._pending`（`/oauth/start` 到 `/oauth/callback` 之間
暫存 state 的字典）刻意存原始 `room_id`、不是 `account_key`，就是為了讓
`oauth_callback` 能正確找回這個房間的資料夾——如果哪裡不小心把 lowercase 過的
key 當成 `room_id` 傳給 `Settings.room_google_dir()`，在 Linux（case-sensitive
檔案系統）上會靜靜地建出另一個空資料夾，跟這個房間真正的 container 掛載的資料夾
對不上。

### 影響既有房間

- **改 Google 相關設定要重建房間 container**：`_build_volume_config` 只在
  container **建立**當下決定要不要掛這個房間的 `google/` 資料夾——先前用停用狀態
  建立的房間，之後補上 `GOOGLE_OAUTH_PUBLIC_URL` 跟 credentials 也不會自動補掛，
  需要 `docker rm -f hermes_<room_id>` 重建。
- **write-once 對 Google MCP 一樣適用**：`gmail`／`drive`／`google-calendar` 三個
  manifest 都有 `requires_google_oauth: true`，`_ensure_mcp_seed` 只在
  `Settings.google_oauth_enabled` 為真時才會 seed 它們——在停用狀態下建立的房間，
  即使之後啟用了 Google 整合，也不會回頭幫它補 seed，一樣要重建房間。
- **`rm -rf data/<room_id>` 會把這個房間的 Google 授權一併清空**：因為
  `tokens.json` 跟該房間自己的憑證副本都在這個資料夾底下，這是刻意的設計（逐房隔離的
  完整理由見 `docs/google-workspace-integration-summary.md`），不是遺漏。詳見下方
  「疑難排解」的「完整重置一個房間」。

### 本機開發：一次性授權

有瀏覽器的開發機可以跳過走 LINE 授權，直接用腳本產生 token：

```bash
uv run python scripts/google_reauth.py U_LOCAL_TEST
```

會存進 `data/U_LOCAL_TEST/google/tokens.json`（`room_id` 保留原始大小寫當資料夾
名，dict 裡的 key 才轉小寫），並把 `--credentials` 指到的 Desktop 憑證複製一份到
同一個資料夾，讓這個房間的 container 掛載後找得到。詳細用法／路徑覆寫見
`scripts/google_reauth.py --help`。

## 疑難排解

> 這節是**部署/建置期**一次性的坑。服務跑起來之後，日常「這則訊息為什麼卡住／
> agent 用了什麼工具／MCP 為什麼噴錯」這類**運行期** debug，見
> [`docs/troubleshooting.md`](docs/troubleshooting.md)（含一鍵診斷腳本
> `scripts/debug_room.py`）。

- **compose 啟動直接失敗**：`hermes_global_net` 沒建（network 宣告為 `external`），
  先 `docker network create hermes_global_net`。
- **host 模式連 Hermes 容器 timeout**：忘了把 `ROUTER_IN_DOCKER` 設 `false`，
  router 在用容器名連線，host 上解析不到。
- **`test_webhook.py` 一直回報「Container 不存在」，router 回應卻是 200**：先看 router
  自己的 terminal（不是 container log）——`_process_and_reply` 對容器編排失敗只會
  log、不會讓 `/webhook` 的回應變成非 200，所以 `test_webhook.py` 看不到真正的錯誤。
  最常見原因是 host 模式下沒設 `DATA_DIR`：預設值 `/app/data` 在宿主機上通常不存在也
  不可寫，log 會看到 `[Errno 30] Read-only file system: '/app'`；解法是把 `DATA_DIR`
  設成跟 `HOST_DATA_DIR` 一樣的絕對路徑（見上方環境變數說明）。
- **第一次訊息很久才回**：Hermes 容器首次啟動要 30–60 秒（s6 + skill sync）屬正常；
  慢機器上 `_wait_until_ready` 的 60 秒 timeout 偶爾不夠，可調 `container_manager.py`。
- **改了掛載來源的 symlink 沒生效**：Docker bind mount 在**建容器時**就把 symlink
  解析成實體路徑，之後切 symlink 對既有容器無效，`docker restart` 也不會重新解析——
  必須 recreate 容器。
- **改了 config.yaml / skills / MCP 設定沒生效**：Hermes 沒有熱載入，
  restart 該房間容器才會生效。
- **房間建立「看似成功」（container 活著、`/health` 200），但每次對話都 500**：跟上面
  `DATA_DIR` 沒設的失敗模式不一樣——這種是**安靜的失敗**，先查 host 模式下
  `HERMES_TEMPLATES_DIR` 是否也覆寫成 repo 的 `src/hermes` 絕對路徑（沒設一樣會拿
  `/app/hermes-templates` 預設值，host 上不存在）。router 自己的 terminal 會有一行
  `ERROR ... Missing config.yaml template at ... skipping room`，但因為容器照樣建立、
  照樣通過健康檢查，這行 log 很容易被忽略；等到真的傳訊息才會在 `/v1/chat/completions`
  上看到 500，這時候 `docker logs hermes_<room_id>` 會看到一堆
  `FileNotFoundError: /opt/data/logs/...`、`sqlite3.OperationalError: unable to open
  database file`（Hermes 自己該補完整的 `logs/`、`cron`、`kanban.db` 全部沒有東西可以
  依附，因為房間根本沒有正確的 `config.yaml`）。
- **`docker run` 建容器失敗，訊息是 `pull access denied` 或建了但
  `exec: "gateway": executable file not found in $PATH`**：先確認 `HERMES_IMAGE`
  指到的 image **真的是**用 `docker build -f Dockerfile.hermes` 建出來的，不是隨手
  retag 一個名字很像但來源不同的 image（例如舊測試留下的 mock stand-in）。驗法：
  `docker inspect <image> --format '{{.Config.Entrypoint}}'` 應該要是
  `[/init /opt/hermes/docker/main-wrapper.sh]`；如果是空的 `[]`，代表這不是從
  `nousresearch/hermes-agent` 衍生出來的 image，`command=["gateway","run"]` 會直接
  找不到執行檔——retag 只會把「image 不存在」的錯誤換成這個更難查的錯誤，該重新
  build 才對。
- **本機起了 router 卻完全沒反應，ngrok 卻顯示 200/400 有打進來**：檢查 port 8000
  是不是被另一個 process 卡住（尤其手上如果有這個 repo的多份 checkout，很容易忘記
  關掉舊的 `fastapi dev`）：`lsof -nP -iTCP:8000 -sTCP:LISTEN`。同一個 port 號，
  `127.0.0.1:8000`（具體位址）會比 `0.0.0.0:8000`（萬用位址）優先攔截本機流量，
  所以就算你剛啟動的新 process 有正常跑起來，舊 process 沒關掉一樣會把 request 搶走。
- **要完整重置一個房間（不只是改設定，是想從零重新建立）**：container 和資料夾要
  **一起**清掉，只清其中一個會變成殭屍狀態（container 活著但掛載的資料夾是空的，
  或資料夾在但 container 名稱衝突建不了新的）：
  ```bash
  docker rm -f hermes_<room_id>
  rm -rf data/<room_id>
  ```
  下一次該房間收到訊息時會完整重新走一次 seed 流程（`config.yaml`／`mcp`／
  `plugins`／`google`）。**這也會把該房間的 Google 授權一併清空**（`tokens.json`
  跟該房間自己的憑證副本都在 `data/<room_id>/google/` 底下，是刻意設計，見上方
  「Google Workspace 整合」）——使用者要重新點一次授權連結。GCP 端的
  `client_secret`／舊 `refresh_token` 不受影響，只是本地不再記得它。

## 指令速查

| 指令 | 說明 |
|------|------|
| `uv run pytest` | 執行所有測試 |
| `uv run pytest --cov=src --cov-report=term-missing` | 含覆蓋率 |
| `uv run mypy src/` | 型別檢查 |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | 格式化 |
| `uv run fastapi dev src/alice_office_router/main.py` | 開發伺服器（host 模式） |
| `uv run python scripts/test_webhook.py --user-id U_LOCAL_TEST --text "..."` | 模擬 LINE 訊息打整條路 |
| `uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST` | 監看**單一房間自己的副本**，存檔自動 restart 該房間 |
| `uv run python scripts/dev_sync_src.py` | 監看 **repo 樣板**，變動時強制推到**所有已存在房間**再 restart（dev 專用，會覆蓋房間副本） |
| `uv run python scripts/google_reauth.py <room_id>` | 本機一次性 Google 授權（見「Google Workspace 整合」） |
| `uv run python scripts/debug_room.py <room_id>` | 印出單一房間的診斷快照（container 狀態、docker logs、各 log 檔 tail、關鍵檔案存在性，見 `docs/troubleshooting.md`） |

提交前必跑：

```bash
uv run ruff check . && uv run mypy src/ && uv run pytest
```

## 專案結構

```
alice-office-router/
├── src/
│   └── alice_office_router/
│       ├── main.py              # FastAPI app factory + lifespan；mount enabled_adapters() 的 routers
│       ├── core.py              # process_inbound：channel-free gate → 容器 → agent → list[str]
│       ├── hermes_client.py     # 呼叫 Hermes 容器的 /v1/chat/completions
│       ├── container_manager.py # Docker 容器動態管理
│       ├── google_oauth.py      # Google OAuth 路由 + 授權 gate（見「Google Workspace 整合」）
│       ├── config.py            # pydantic-settings 設定
│       └── channels/            # channel adapters（每個通道自己的 wire format）
│           ├── __init__.py      # enabled_adapters(config)
│           ├── base.py          # InboundMessage、ChannelAdapter Protocol
│           └── line/
│               ├── adapter.py   # LineAdapter：POST /webhooks/line 端點 + 回覆送出（reply token 優先，push 兜底）
│               ├── verify.py    # LINE HMAC-SHA256 簽章驗證
│               ├── client.py    # 呼叫 LINE Reply/Push Message API + Content API 下載媒體
│               ├── format.py    # Markdown 去除 + 長文分段（LINE bubble 限制）
│               ├── dedup.py     # Webhook event 去重（in-memory）
│               └── events.py    # LINE webhook 事件 pydantic model + inbound 文字解析
├── tests/
│   ├── conftest.py
│   ├── test_core.py
│   ├── test_hermes_client.py
│   ├── test_container_manager.py
│   ├── test_google_oauth.py
│   ├── test_hermes_shared_node_deps.py  # 檢查各 MCP package.json 與共用 package.json 同步
│   └── channels/line/           # LINE wire-format 測試（test_adapter/verify/client/format/dedup/events）
├── src/hermes/                  # MCP / plugin 原始碼樣板（seed 進每個房間，見上方「C. Plugin / MCP」）
│   ├── config.template.yml      # 每個新房間 config.yaml 的樣板（_ensure_config_yaml 讀取後 .format() 填值）
│   ├── mcp/
│   │   ├── package.json         # 所有 MCP 共用依賴（烤進 image 的 /opt/node_modules）
│   │   ├── package-lock.json    # 對應鎖版檔（image build 用 npm ci）
│   │   ├── secretary/           # todo/meeting/translate/... MCP server（Node ESM stdio）
│   │   ├── gmail/                # Gmail MCP server（Python stdio，requires_google_oauth）
│   │   ├── drive/                # Google Drive MCP server（Python stdio，requires_google_oauth）
│   │   └── google-calendar/      # thin registration，實際 server 是烤進 image 的 npm 套件
│   ├── plugin/
│   │   └── local-tools/         # 台灣薪資/法規/數學/記憶/OCR/瀏覽器 工具包
│   ├── runtime/                 # 共用 Python 工具環境（烤進 image 的 /opt/tools/.venv）
│   │   ├── pyproject.toml       # third-party 套件清單（sympy/pymupdf/selenium）
│   │   ├── uv.lock              # 對應鎖版檔（image build 用 uv sync --locked）
│   │   └── profile-tools.sh     # login shell 用，export TOOLS_PYTHON + PATH
├── scripts/
│   └── test_webhook.py          # 手動 end-to-end 測試腳本
├── docs/                        # 架構設計文件（見下方各章節的「詳見 docs/...」連結）
├── docker-compose.yml
├── Dockerfile                   # Router image
├── Dockerfile.hermes            # 衍生 Hermes image（預裝 plugin + MCP 共用依賴，production 用）
├── pyproject.toml
└── .env.example
```

## 環境變數說明

| 變數 | 必填 | 說明 |
|------|------|------|
| `LINE_CHANNEL_SECRET` | ✅ | LINE Webhook 簽章驗證用 |
| `LINE_CHANNEL_ACCESS_TOKEN` | ✅ | Router 自己用來呼叫 LINE Push Message API（不會傳入 Hermes 容器） |
| `HOST_DATA_DIR` | ✅ | 宿主機上 `data/` 的絕對路徑，用於 Docker Volume 掛載 |
| `HERMES_API_SERVER_KEY` | ✅ | Router 與每個 Hermes 容器共用的 Bearer 密鑰（容器的 `api_server` platform 靠它啟用與驗證） |
| `DATA_DIR` | ⚠️ | Router 進程自己讀寫房間資料夾（`mkdir`、寫 `config.yaml`、seed mcp/plugins）用的路徑，預設 `/app/data`。**Container 化部署免設**（router 自己也在容器裡，`/app/data` 就是掛載進來的路徑）；**host 模式（`ROUTER_IN_DOCKER=false`）必填**，要設成跟 `HOST_DATA_DIR` 一樣的絕對路徑，否則 router 會嘗試在宿主機上建立 `/app/data`（通常不存在也不可寫）而整個建房間失敗 |
| `HERMES_TEMPLATES_DIR` | ⚠️ | Router 進程自己讀取 MCP/plugin 樣板（`mcp/<name>/`、`plugin/<name>/`）用的路徑，預設 `/app/hermes-templates`。**Container 化部署免設**（compose 已把 `./src/hermes` 掛進 router 自己的容器）；**host 模式必填**，要設成 repo 的 `src/hermes` 絕對路徑，道理跟 `DATA_DIR` 一樣 |
| `HERMES_IMAGE` | | Hermes Agent 映像（預設 `nousresearch/hermes-agent`，等同 `latest`——請改成 pin 版本 tag，如 `nousresearch/hermes-agent:v2026.4.16`） |
| `HERMES_NETWORK` | | Docker 內網名稱（預設 `hermes_global_net`） |
| `HERMES_INTERNAL_PORT` | | Hermes Agent `api_server` 監聽 Port（預設 `8642`） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | | 共用 LLM 後端設定，自動寫入每個新房間的 `config.yaml` |
| `ROUTER_IN_DOCKER` | | Router 是否跑在 Docker 內（預設 `true`）；本機開發用 `uv run uvicorn` 時設為 `false`，容器會改為發布隨機 host port |
| `DEFAULT_PLUGINS` | | 寫入每個新房間 config.yaml 的預設 plugin 清單（逗號分隔，預設 `local-tools`），名稱需對應 `HERMES_TEMPLATES_DIR/plugin/` 底下已 seed 的目錄名 |
| `GOOGLE_OAUTH_PUBLIC_URL` | | 這個 router 的公開 HTTPS base URL（不含結尾斜線）。留空（預設）＝ Google Workspace 整合停用，見「[Google Workspace 整合](#google-workspace-整合)」 |
| `GOOGLE_OAUTH_GATE` | | 預設 `true`。設 `false` 時 Google OAuth 路由照常運作，只是不擋任何房間的訊息 |

## 安全性

- 每個 Webhook 請求均驗證 LINE HMAC-SHA256 簽章，驗證失敗回傳 `400`。
- 各聊天室的 Hermes Agent 容器僅掛載自己的 Volume（`/opt/data`），容器間硬碟資料完全隔離；使用者傳送的圖片/檔案/語音/影片也是落在各自房間的 `incoming/` 子目錄下，同樣不互通。啟用 Google 整合時，`/opt/google-workspace`（含 tokens、GCP 憑證副本）也是逐房掛載，同一部署下不同房間的 container 讀不到彼此的 Google 授權——見「[Google Workspace 整合](#google-workspace-整合)」。
- Hermes 容器完全不接觸 LINE 憑證，只透過 `HERMES_API_SERVER_KEY` 與 Router 的內部 API 通訊；`api_server` 本身只在 Docker 內網（`hermes_global_net`）可達。
- `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`HERMES_API_SERVER_KEY` 僅存於 `.env`，不進版控。
