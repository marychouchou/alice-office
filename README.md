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
HOST_PLUGINS_DIR=/absolute/path/to/alice-office-router/plugins
HOST_SECRETARY_MCP_DIR=/absolute/path/to/alice-office-router/secretary-mcp  # 要改 MCP 才需要
HERMES_IMAGE=alice-hermes-agent:v1
HERMES_API_SERVER_KEY=change-me            # openssl rand -hex 32
LLM_BASE_URL=change-me                     # 唯一不能假的：可用的 OpenAI-compatible endpoint
LLM_API_KEY=change-me
LLM_MODEL=change-me
```

> `HOST_*` 必須是**宿主機**的絕對路徑，Docker 掛載 volume 時需要用到。
> `DATA_DIR` 預設是 `/app/data`（給 router 自己也跑在容器裡的部署模式用）——**host 模式
> （`uv run fastapi dev`）下必須另外覆寫成跟 `HOST_DATA_DIR` 一樣的絕對路徑**，因為
> router 進程會直接在宿主機上對這個路徑做 `mkdir`／寫 `config.yaml`；沒設的話會拿預設值
> `/app/data`，在 macOS／Linux host 上通常不存在也不可寫，會在建立房間時整個失敗，
> 且錯誤只會出現在 router 自己的 terminal（背景任務吞掉例外），對 `test_webhook.py` 呼叫方
> 看起來像是「container 一直不存在」而非明確報錯。
> `HERMES_API_SERVER_KEY` 是 router 與每個 Hermes 容器共用的密鑰。
> `LLM_*` 是共用的 LLM 後端設定，會自動寫入每個新房間的 `config.yaml`。

### 3. 建立 Docker 網路、準備 Hermes image

```bash
docker network create hermes_global_net
docker build -f Dockerfile.hermes -t alice-hermes-agent:v1 .   # 含 plugin 依賴與 secretary-mcp
```

> `hermes_global_net` 在 `docker-compose.yml` 中宣告為 `external`，沒先建立會直接啟動失敗。
>
> 趕時間可以跳過 build，先 `docker pull nousresearch/hermes-agent:<pinned-tag>`（Docker Hub
> 公開 image，免權限）填進 `HERMES_IMAGE`——local-tools 的 4 個 stdlib 工具能動，但
> math／OCR／webdriver 與 secretary-mcp 不行，差別見「[預裝 Plugin](#預裝-pluginlocal-tools)」。
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
（sessions、memories、skills…）——這個目錄就是該房間的「記憶」，容器可以隨時砍掉重建而不失憶，
但不要手動修改裡面的狀態檔。

agent 的回覆去哪看：假 user id 推不回真的 LINE（router log 出現 `Failed to push LINE reply`
屬預期），所以看 `docker logs -f hermes_U_LOCAL_TEST` 與 router terminal 的 log。

### 5. 日常開發迴圈

```bash
# terminal A：router
uv run fastapi dev src/alice_office_router/main.py

# terminal B：watcher——監看 plugins/ 與 secretary-mcp/，存檔自動 restart 測試房間
uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST

# terminal C：改 code → 存檔 → 等 watcher 顯示 restart 完成（warm restart 約 10–15 秒）
#            → 送訊息驗證
vim plugins/local-tools/tools.py              # 或 secretary-mcp/tools/*.mjs
uv run python scripts/test_webhook.py --user-id U_LOCAL_TEST --text "呼叫 math 工具，expression=\"2+2\""
```

- 改 **router code**（`src/`）：`fastapi dev` 自己會 reload，不用動任何容器。
- 改 **plugin / MCP**：watcher 自動 restart 測試房間。更細的生效條件（哪些要 restart、
  哪些要 rebuild）見「[測試 plugins 修改](#測試-plugins-修改)」與
  「[測試 secretary-mcp 修改](#測試-secretary-mcp-修改)」。
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

**預設路徑：MCP server 做成獨立的 HTTP/SSE 容器**（sibling container），不進 Hermes image。
image 裡雖有 node / npx / uvx 可跑 stdio MCP，但那樣每個房間容器都要各自複製一份 runtime；
HTTP/SSE 一份服務所有房間共用：

1. 開發並 `docker build`，`docker run --network hermes_global_net` 起起來
2. 在測試房間的 `data/<room_id>/config.yaml` 加 MCP 設定（指向 `http://<container-name>:<port>`）
3. `docker restart hermes_<room_id>`，用 `test_webhook.py` 驗證 agent 呼叫得到該 tool

#### 現況：secretary-mcp 是例外路徑

`secretary-mcp/` 目前**不是**走上面的「獨立 HTTP/SSE 容器」路徑，而是 stdio + 烤進
`Dockerfile.hermes`（`/opt/secretary-mcp/`），每個房間的 Hermes 容器各自 spawn 一份
（`SECRETARY_LINE_USER_ID` = `room_id`，見 `container_manager.py` 的 `_MCP_SECTION_TEMPLATE`）。
這是文件裡說的「必須跑在 Hermes 進程內」例外情況的變體——嚴格說它不是真 in-process
plugin，只是圖方便先這樣接。之後若要遷移成共用 HTTP/SSE 容器，需要先解決
tenant 識別（目前靠 process 啟動時的 env var 分房間，共用容器要改成 per-request 識別）。

因為源碼烤在 image 裡，改 `secretary-mcp/` 程式碼預設要重 build `Dockerfile.hermes`
才會生效。Dev 環境可設 `HOST_SECRETARY_MCP_DIR`（見環境變數表）bind mount
`server.mjs` + `tools/` 覆蓋 image 內版本，`docker restart` 即可測試新程式碼；
`node_modules` 依然吃 image 內建的，套件版本變動時還是得重 build。

##### 測試 secretary-mcp 修改

**Level 0（最快，不碰 Docker/Hermes）**——用官方 MCP inspector 直接對 `server.mjs` 打 stdio protocol：

```bash
cd secretary-mcp && npm install   # 第一次要裝依賴
SECRETARY_LINE_USER_ID=test_room npx @modelcontextprotocol/inspector node server.mjs
```

開瀏覽器 UI，可直接呼叫個別 tool、驗 schema、看回傳值，不用經過 Hermes。

**Level 1（透過 Hermes 容器驗證，用 dev override）**：

1. `.env` 設 `HOST_SECRETARY_MCP_DIR=/absolute/path/to/alice-office-router/secretary-mcp`
2. **注意**：如果測試房間的 `hermes_<room_id>` 容器**在設定這個變數之前就已存在**，
   `docker restart` 不會套用新 mount——volume 掛載是建立容器當下就固定的。
   第一次啟用要先 `docker rm -f hermes_<room_id>`，讓 router 下次收到訊息時重新建立容器
   （帶上新 mount）。全新房間則不用這步，建立時就會直接帶上。
3. 之後改 `server.mjs` / `tools/*.mjs`，只要 `docker restart hermes_<room_id>` 就會生效
   （Hermes gateway 重啟時重新 spawn MCP server process，讀到新程式碼）——這步可以用
   `uv run python scripts/watch_restart.py` 自動化，存檔即觸發，見下方「測試 plugins 修改」
4. `uv run python scripts/test_webhook.py` 送會觸發 secretary tool 的訊息（例如「幫我加一筆待辦」），
   `docker logs hermes_<room_id>` 找 `[secretary-mcp] ready; lineUserId=...` 確認 spawn 成功、有無報錯

**Level 2（完整驗證，套件有變動時必跑）**：改了 `package.json`（新增/升級依賴）時，
dev override 的 `node_modules` 還是吃 image 內建的，必須重 build：

```bash
docker build -f Dockerfile.hermes -t alice-hermes-agent:v2 .
# .env 改 HERMES_IMAGE=alice-hermes-agent:v2，docker rm -f 測試房間容器重建
```

#### 預裝 Plugin（local-tools）

Repo 的 `plugins/local-tools/` 是一套 Hermes standalone plugin（台灣薪資計算、法規查詢、工程計算機、長期記憶、AI 生態系索引、OCR、瀏覽器自動化），**每個 Hermes 容器啟動時自動掛載為預設工具**。掛載方式：

- **原始碼**：`plugins/` 目錄以 read-only volume 掛載到容器的 `/opt/data/plugins/`（所有房間共用）
- **啟用**：每個新房間的 `config.yaml` 模板自動寫入 `plugins.enabled: [local-tools]`
- **執行資料**（SQLite、快取）：落在各房間的 `/opt/data/local-tools-data/`（房間隔離）

工具的 Python 依賴分為兩類：

| 工具 | 依賴 | 上游 image 是否內建 |
|------|------|---------------------|
| hr / law / longmem / research | 純 stdlib | ✅ 直接可用 |
| math | `sympy` | ❌ 需衍生 image |
| image_ocr | `pymupdf` + 外部 Vision API | ❌ 需衍生 image + API server |
| webdriver | `selenium` + geckodriver + Firefox | ❌ 需衍生 image（plugin 自動隱藏） |

**Production 建法**——用 `Dockerfile.hermes` 建衍生 image 預裝 sympy + pymupdf：

```bash
docker build -f Dockerfile.hermes -t alice-hermes-agent:v1 .
# .env 設 HERMES_IMAGE=alice-hermes-agent:v1
```

> 原始碼仍走 volume 掛載，不 bake 進 image——改 plugin 程式碼只需 `docker restart`，
> 不用重 build。只有依賴變動時才需要重 build 衍生 image。
>
> 一般開發時用上游 `nousresearch/hermes-agent` 即可，4 個 stdlib 工具直接可用。

只有當功能必須跑在 Hermes **進程內**（真 plugin，不是 MCP）才走衍生 image：
`FROM nousresearch/hermes-agent:<pin>`，改 `HERMES_IMAGE` 逐房重建。
這條路每次升級 Hermes 都要 rebase，成本高，沒必要不要走。

##### 測試 plugins 修改

**Level 0（最快，不碰 Docker/Hermes）**——每個 tool 是一支獨立可執行的 CLI script
（`tools.py` 用 `subprocess.run([PYTHON, script, *argv])` 呼叫，吃 CLI args、吐 JSON stdout），
可以直接跑，邏輯對不對這層就測得完：

```bash
python3 plugins/local-tools/scripts/hr/alice-payroll-engine.py --help
python3 plugins/local-tools/scripts/hr/alice-payroll-engine.py <實際參數>
```

**Level 1（驗證 Hermes 真的呼叫得到 tool）**：

1. 確保測試房間容器已存在（`plugins/` 本來就是 hot-mount，host 模式、容器模式都免 rebuild）
2. `docker restart hermes_<room_id>`——新加的 tool 或改了 `plugin.yaml` / `schemas.py` 需要
   restart 才生效；純改 script 內容其實每次呼叫都是重新 spawn subprocess，通常不用重啟，
   但 restart 保險
3. `uv run python scripts/test_webhook.py` 送一句會觸發該 tool 的訊息，看 agent 回覆
4. 有問題就 `docker logs -f hermes_<room_id>` 看 stderr

**不想每次手動打 restart？** `scripts/watch_restart.py` 會輪詢 `plugins/`（有設
`HOST_SECRETARY_MCP_DIR` 的話連 secretary-mcp 一起）的檔案異動，存檔自動
`docker restart hermes_<room_id>`：

```bash
uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST
```

只是把「你自己打 restart」自動化，容器怎麼建立、掛載什麼都還是
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

## 疑難排解

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
| `uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST` | 監看 extension 原始碼，存檔自動 restart |

提交前必跑：

```bash
uv run ruff check . && uv run mypy src/ && uv run pytest
```

## 專案結構

```
alice-office-router/
├── src/
│   └── alice_office_router/
│       ├── main.py              # FastAPI app factory + lifespan
│       ├── router.py            # POST /webhook 端點 + 取得回覆並 push 回 LINE
│       ├── line_verify.py       # LINE HMAC-SHA256 簽章驗證
│       ├── line_client.py       # 呼叫 LINE Reply/Push Message API + Content API 下載媒體
│       ├── line_format.py       # Markdown 去除 + 長文分段（LINE bubble 限制）
│       ├── line_dedup.py        # Webhook event 去重（in-memory）
│       ├── hermes_client.py     # 呼叫 Hermes 容器的 /v1/chat/completions
│       ├── container_manager.py # Docker 容器動態管理
│       └── config.py            # pydantic-settings 設定
├── tests/
│   ├── conftest.py
│   ├── test_line_verify.py
│   ├── test_line_client.py
│   ├── test_line_format.py
│   ├── test_line_dedup.py
│   ├── test_hermes_client.py
│   ├── test_router.py
│   └── test_container_manager.py
├── plugins/                     # Hermes 預裝 plugin（volume 掛載到每個容器）
│   └── local-tools/             # 台灣薪資/法規/數學/記憶/OCR/瀏覽器 工具包
├── scripts/
│   └── test_webhook.py          # 手動 end-to-end 測試腳本
├── docs/                        # 設計文件（不進版控，clone 不會有；實質內容以本 README 為準）
├── docker-compose.yml
├── Dockerfile                   # Router image
├── Dockerfile.hermes            # 衍生 Hermes image（預裝 plugin 依賴，production 用）
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
| `DATA_DIR` | ⚠️ | Router 進程自己讀寫房間資料夾（`mkdir`、寫 `config.yaml`）用的路徑，預設 `/app/data`。**Container 化部署免設**（router 自己也在容器裡，`/app/data` 就是掛載進來的路徑）；**host 模式（`ROUTER_IN_DOCKER=false`）必填**，要設成跟 `HOST_DATA_DIR` 一樣的絕對路徑，否則 router 會嘗試在宿主機上建立 `/app/data`（通常不存在也不可寫）而整個建房間失敗 |
| `HERMES_IMAGE` | | Hermes Agent 映像（預設 `nousresearch/hermes-agent`，等同 `latest`——請改成 pin 版本 tag，如 `nousresearch/hermes-agent:v2026.4.16`） |
| `HERMES_NETWORK` | | Docker 內網名稱（預設 `hermes_global_net`） |
| `HERMES_INTERNAL_PORT` | | Hermes Agent `api_server` 監聽 Port（預設 `8642`） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | | 共用 LLM 後端設定，自動寫入每個新房間的 `config.yaml` |
| `ROUTER_IN_DOCKER` | | Router 是否跑在 Docker 內（預設 `true`）；本機開發用 `uv run uvicorn` 時設為 `false`，容器會改為發布隨機 host port |
| `HOST_PLUGINS_DIR` | | 宿主機上 `plugins/` 的絕對路徑，用於 Docker Volume 掛載（compose 自動設 `${PWD}/plugins`） |
| `HOST_SECRETARY_MCP_DIR` | | **Dev only**。宿主機上 `secretary-mcp/` 的絕對路徑；設定後會把 `server.mjs` + `tools/` bind mount 覆蓋 image 內烤好的版本，改程式碼只需 `docker restart` 不用重 build（`node_modules` 仍用 image 內建的）。Production 留空——客戶主機沒有這份 repo 原始碼 |
| `DEFAULT_PLUGINS` | | 寫入每個新房間 config.yaml 的預設 plugin 清單（逗號分隔，預設 `local-tools`） |

## 安全性

- 每個 Webhook 請求均驗證 LINE HMAC-SHA256 簽章，驗證失敗回傳 `400`。
- 各聊天室的 Hermes Agent 容器僅掛載自己的 Volume（`/opt/data`），容器間硬碟資料完全隔離；使用者傳送的圖片/檔案/語音/影片也是落在各自房間的 `incoming/` 子目錄下，同樣不互通。
- Hermes 容器完全不接觸 LINE 憑證，只透過 `HERMES_API_SERVER_KEY` 與 Router 的內部 API 通訊；`api_server` 本身只在 Docker 內網（`hermes_global_net`）可達。
- `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`HERMES_API_SERVER_KEY` 僅存於 `.env`，不進版控。
