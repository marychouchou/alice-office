# 疑難排解：Router / Container / Agent 內部 Debug

聚焦說明：**服務已經跑起來、訊息也送得進去之後**，怎麼追「發生了什麼事」——
router 有沒有收到 webhook、容器活不活著、agent 有沒有收到訊息、agent 用了什麼
工具、MCP server 為什麼掛掉。這些問題單靠 `docker ps` 看不出來，因為 log 散在
router 自己的 terminal、`docker logs hermes_<room_id>`、還有
`data/<room_id>/logs/` 底下好幾個檔案，三個地方都要看才拼得出完整故事。

**跟 README「疑難排解」的分工**：README 那節是**部署/建置期**一次性的坑（network
沒建、host 模式路徑沒設、image 建錯、port 被佔用……），本文件是**日常運行期**
反覆會遇到的排查（哪個房間的哪則訊息為什麼卡住、agent 用了什麼工具、MCP 為什麼
噴錯）。兩者互補，不重複。

以下每個檔案/行為都是實地讀過本機一個真實房間（含超過一次對話、呼叫過
Google Calendar／Drive／Gmail 工具）之後確認的，不是憑 Hermes 官方文件猜的。

## 1. Log 地圖

| 你想知道什麼 | 去哪裡看 | 指令 |
|---|---|---|
| Router 有沒有收到這個 webhook | Router 自己的 log（host 模式＝terminal 直接印；container 模式＝`docker compose logs`） | `docker compose logs -f webhook_router`（或本機開發的 `uv run fastapi dev` terminal），找 uvicorn access log 的 `"POST /webhook HTTP/1.1" 200`／`400` |
| 房間 container 活著沒、activty 狀態 | `docker ps` | `docker ps -a --filter name=hermes_<room_id>` |
| Hermes agent（api_server）有沒有收到這次請求 | `docker logs`，或房間自己的 `agent.log` | `docker logs --tail 50 hermes_<room_id>`；或 `tail data/<room_id>/logs/agent.log`，找 `aiohttp.access: ... "POST /v1/chat/completions HTTP/1.1" 200` |
| agent 這次用了什麼工具、耗時、輸出大小 | `agent.log` 的 `agent.tool_executor` 行 | `grep tool_executor data/<room_id>/logs/agent.log` |
| agent 這次工具呼叫的**完整參數與回傳內容** | `state.db`（sqlite） | 見 2.2 節的 sqlite 指令 |
| MCP server 為什麼掛掉/沒註冊成功 | `mcp-stderr.log`、`agent.log` 的 `tools.mcp_tool` 行 | `tail -n 50 data/<room_id>/logs/mcp-stderr.log` |
| 容器為什麼起不來 / health check timeout | `container-boot.log`、`gateway-exit-diag.log`、`docker logs` | 見 2.4 節 |
| Google OAuth 卡在哪一步 | 房間的 `google/` 目錄、router log 的 oauth 錯誤行 | 見 2.5 節 |
| router 自己有沒有丟例外（容器編排／呼叫 agent／回推 LINE 失敗） | router 自己的 log | 見下方「Router 自己會記錄的行為」 |

### Router 自己會記錄的行為

`main.py` 只呼叫 `logging.basicConfig(level=logging.INFO)`，沒有另外設檔案
handler，所以 router 的 log **就是它的 process 標準輸出**：host 模式是 terminal，
container 模式是 `docker compose logs webhook_router`（docker 預設的 `json-file`
log driver 已經在幫你把它寫進磁碟，見第 4 節）。目前 router（`channels/line/adapter.py`、
`core.py`、`channels/line/events.py`）會記錄的行是：

- `Skipping duplicate LINE webhook event {event_id}`（INFO，去重擋掉）
- `Skipping LINE message event with unresolvable room id`（WARNING）
- `Ignoring unsupported LINE message type: ...`（INFO）
- `Failed to download LINE ... content ...`（ERROR，媒體下載失敗）
- `Failed to get/create container for room ...`（ERROR，容器編排失敗）
- `Hermes agent request failed for room ...`（ERROR，呼叫 agent 失敗）
- `LINE reply token rejected for room ...; falling back to push`（INFO，正常
  fallback，不是錯誤）
- `Failed to push LINE reply for room ...`（ERROR，LINE Push 也失敗）
- `Failed to push Google OAuth notice for room ...`（ERROR）

`container_manager.py` 另外會記錄 `Creating new container for room`、
`Seeded template [...] into ...`、`Container ... created.`、
`Waiting for ... to become ready...`、`Docker API error for container ...`。

注意：`line_webhook` 本身在簽章驗證通過、events 解析完之後**沒有**額外印一行
「收到 webhook」——訊號是 uvicorn 的 access log 那一行，不是應用層的 log。

## 2. 症狀 → 排查流程

### 2.1 傳訊息沒回應（端到端）

1. 確認訊息真的打到 router：`docker compose logs --tail 50 webhook_router`（或
   host 模式看 terminal），找 `POST /webhook`。完全沒出現 → 問題在 LINE 平台／
   ngrok／domain，不是這個 repo 的問題。出現但是 `400` → LINE 簽章驗證失敗
   （`LINE_CHANNEL_SECRET` 錯，或中間有東西改了 raw body）。
2. 確認事件沒被去重或判定成無效 room id：同一段 log 找
   `Skipping duplicate LINE webhook event` / `Skipping LINE message event with
   unresolvable room id`。
3. 確認房間 container 存在且 running：
   `docker ps -a --filter name=hermes_<room_id>`。不存在/沒起來 → 回頭看 router
   log 的 `Creating new container for room` 有沒有接著 `Failed to get/create
   container for room`（通常是 `DATA_DIR`／`HERMES_TEMPLATES_DIR` 沒設對，見
   README「疑難排解」）。
4. 若這個部署啟用了 Google OAuth gate，確認訊息沒被擋在 gate：見 2.5 節。
5. 確認 agent 真的收到請求：`docker logs --tail 50 hermes_<room_id>` 或
   `tail data/<room_id>/logs/agent.log`，找 `/v1/chat/completions`。完全沒有 →
   `ask_hermes_agent` 這次 HTTP call 可能還沒發出或連線失敗，回頭看 router log 的
   `Hermes agent request failed for room`。
6. 有收到請求但 agent 側報錯：看 `data/<room_id>/logs/errors.log`（WARNING 以上）
   跟 `agent.log` 該次 `session=` 附近的行——`agent.conversation_loop` 會記錄
   `API call #N` 與 `Turn ended`，`Turn ended` 沒出現代表這次 turn 卡住或還在跑。
7. 確認回覆真的送回 LINE：router log 找 `LINE reply token rejected ... falling
   back to push`（正常）或 `Failed to push LINE reply for room`（真的失敗，通常是
   `LINE_CHANNEL_ACCESS_TOKEN` 或假的 room_id）。

用 `uv run python scripts/debug_room.py <room_id>` 可以一次印出第 3、5、6 步要看
的東西（容器狀態、docker logs、每個 log 檔的 tail），省掉手動下這幾個指令。

### 2.2 Agent 這次用了什麼工具（最常被問的問題）

`agent.log` 的 `agent.tool_executor` logger 每次工具呼叫**完成**都會記一行摘要：

```
grep tool_executor data/<room_id>/logs/agent.log
# 2026-07-11 10:08:51,510 INFO [<room_id>] agent.tool_executor: tool mcp_google_calendar_get_current_time completed (0.47s, 159 chars)
```

這行只有工具名稱、耗時、回傳字元數——**沒有呼叫參數，也沒有實際回傳內容**。
要看完整內容（模型實際傳了什麼參數、工具實際回了什麼），得查房間自己的
`state.db`（sqlite，Hermes 自己維護的對話狀態）：

```bash
sqlite3 data/<room_id>/state.db "
  SELECT id, role, tool_name, tool_calls, content
  FROM messages
  WHERE role IN ('assistant', 'tool')
  ORDER BY id DESC LIMIT 20;
"
```

- `role='assistant'` 的 `tool_calls` 欄位是模型發出的呼叫 JSON（工具名 +
  arguments）。
- 緊接著那筆 `role='tool'` 的 `content` 欄位就是該次呼叫的**完整回傳內容**，用
  `tool_call_id` 對應到前一筆的呼叫。
- 想看這次對話一開始註冊成功了哪些 MCP 工具：`grep tools.mcp_tool
  data/<room_id>/logs/agent.log`。

### 2.3 MCP tool 呼叫失敗

- 所有 MCP server 共用同一個 `mcp-stderr.log`，每個 server 開機時會印一行分隔
  `===== [ts] starting MCP server 'X' =====`，往下找到對應區塊即可定位是哪個
  server 出錯：`tail -n 80 data/<room_id>/logs/mcp-stderr.log`。
- 手動在房間容器內單獨啟動一個 MCP server（不透過 gateway，直接看 stdio 啟動
  訊息）：
  ```bash
  docker exec -it hermes_<room_id> node /opt/data/mcp/<name>/server.mjs
  # Python MCP（gmail/drive）：
  docker exec -it hermes_<room_id> /opt/tools/.venv/bin/python3 /opt/data/mcp/<name>/server.py
  ```
  能正常等待 stdio 輸入（沒有立刻印 traceback 退出）代表啟動本身沒問題，
  Ctrl+C 結束。
- 只想檢查房間自己那份 MCP 原始碼有沒有語法錯誤，不需要進容器：
  `node --check data/<room_id>/mcp/<name>/server.mjs`。

### 2.4 容器起不來 / health check timeout

1. `docker ps -a --filter name=hermes_<room_id>` 看容器目前狀態
   （`Exited`／`Restarting`／根本沒建立）。
2. `docker logs --tail 100 hermes_<room_id>` —— 最直接的錯誤來源，image
   entrypoint／s6 的錯誤訊息都在這裡。
3. `cat data/<room_id>/logs/container-boot.log` —— 每次 s6 開機一行
   `profile=default prior_state=... action=started`；短時間內狂增行數＝
   crash-loop。
4. `cat data/<room_id>/logs/gateway-exit-diag.log` —— JSON lines，每次
   gateway 啟動/結束各一行 `tag`（`gateway.start` / `asyncio.run.returned
   success=false` / `gateway.exit_nonzero`）。`success=false` 代表 gateway
   process 本身丟例外退出，traceback 要往 `docker logs` 或 `errors.log` 找。
5. 若容器收過 SIGTERM，`data/<room_id>/logs/gateway-shutdown-diag.log` 會有
   當下的 `ps auxf` 快照（含 `dmesg` 段落），可以用來判斷是不是被 OOM kill。
6. 常見成因：`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` 沒設，導致
   `config.yaml` 沒被正確渲染（router log 找 `Missing config.yaml template`）；
   `HERMES_IMAGE` 指到的不是真的用 `Dockerfile.hermes` build 出來的 image
   （`docker inspect <image> --format '{{.Config.Entrypoint}}'` 應該是
   `[/init ...]`，空的代表 image 不對）；host 模式 port 被其他 process 卡住。

`uv run python scripts/debug_room.py <room_id>` 會把第 2、3、4 步的內容一次印
出來。

### 2.5 Google OAuth 卡住

1. 確認這個部署真的啟用了：`GOOGLE_OAUTH_PUBLIC_URL` 有沒有設 + 部署層級的
   `data/_google/gcp-oauth.keys.json` 是否存在（`Settings.google_oauth_enabled`）。
2. 確認房間自己有沒有拿到憑證副本（write-once，由 `ensure_google_seed` 複製）：
   `ls data/<room_id>/google/` 應該看得到 `gcp-oauth.keys.json`。
3. 確認這個房間有沒有完成過授權：`cat data/<room_id>/google/tokens.json` ——
   不存在代表這個房間從沒授權成功過。
4. router log 找 oauth 相關錯誤：`Failed to load Google web credentials for
   room`、`Google OAuth token exchange failed for`、`Failed to read Google
   tokens for account`。
5. 需要本機手動重新走一次授權流程時，用 `uv run python scripts/google_reauth.py
   <room_id>`，或直接開 `<GOOGLE_OAUTH_PUBLIC_URL>/oauth/start?user_id=<room_id>`。

### 2.6 改了 config.yaml / skills / MCP 沒生效

Hermes 沒有熱載入（見 CLAUDE.md「Hermes Container Model」），改完一定要
`docker restart hermes_<room_id>` 才會生效：

- 直接改**房間自己**已 seed 出來的副本（`data/<room_id>/{mcp,plugins}/`）：
  `uv run python scripts/watch_restart.py --room-id <room_id>`，存檔自動 restart
  這一個房間。
- 改的是 **repo 樣板**（`src/hermes/{mcp,plugin}/`、`config.template.yml`）：
  `uv run python scripts/dev_sync_src.py`，會把樣板強制推到**所有已存在房間**再
  restart（僅限開發用，production 不要跑——房間副本是使用者可自由編輯的
  write-once 資料）。
- `skills/` 不受這兩支腳本管——它是 Hermes gateway 自己開機時做的
  manifest-based sync，一樣是 restart 容器後、下次開機才會重新比對。

### 2.7 用 API 通道 curl 進任何房間（不經 LINE 除錯）

第一方 API 通道（`channels/api.py`，設計文件 §4.4）是**不經 LINE 就能打進任何
房間**的除錯入口：走跟 LINE 完全相同的 gate → 容器 → agent 管線，但回覆**同步**
放在 HTTP response、且是 agent 的**原始 markdown**（不剝除、不切塊）——最適合單
獨看「agent 到底回了什麼」，不被 LINE 的泡泡切塊干擾。

先決條件：`.env` 設了 `API_CHANNEL_TOKEN`（留空＝通道不掛載，`/webhooks/api/messages`
會 404）。改完 `.env` 要重啟 router。

**A. 打進一個既有的 `line_*` 房間**（不會驚動真的 LINE 使用者，回覆只回到 curl）：

```bash
curl -s -X POST localhost:8000/webhooks/api/messages \
  -H "Authorization: Bearer $API_CHANNEL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"room_key":"line_U0123456789abcdef0123456789abcdef","text":"你剛剛用了什麼工具？"}'
# → {"replies": ["...agent 原始 markdown..."]}
```

（`room_key` 就是 `docker ps` / `data/` 底下看到的房間鍵，含 `line_` 前綴。）

**B. 開一個全新的 `api_*` 房間**（TUI / mobile 正式使用時各自的房間；`api_<slug>`
的 slug 是 `[a-z0-9-]{1,32}`）：

```bash
curl -s -X POST localhost:8000/webhooks/api/messages \
  -H "Authorization: Bearer $API_CHANNEL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"room_key":"api_dev","text":"回覆一個字：好"}'
```

第一次打某個新 `room_key` 會拉起它自己的 `hermes_<room_key>` 容器（開機 30–60
秒，見 2.4），並 seed 出 `data/<room_key>/`。驗證：`docker ps --filter
name=hermes_api_dev`、`ls data/api_dev/`。錯誤回應：token 不對 → `401`；
`room_key` 不是 `line_<native id>` 或 `api_<slug>`、或 `text` 空白 → `422`。

## 3. 指令速查表

| 想做什麼 | 指令 |
|---|---|
| 一次看某房間的完整診斷快照 | `uv run python scripts/debug_room.py <room_id>` |
| 看某房間容器最後 50 行 log | `docker logs --tail 50 hermes_<room_id>` |
| 看某房間 agent 用了什麼工具 | `grep tool_executor data/<room_id>/logs/agent.log` |
| 看某次工具呼叫完整參數/回傳 | `sqlite3 data/<room_id>/state.db "SELECT tool_name, tool_calls, content FROM messages WHERE role IN ('assistant','tool') ORDER BY id DESC LIMIT 20;"` |
| 看 MCP server 啟動/錯誤訊息 | `tail -n 80 data/<room_id>/logs/mcp-stderr.log` |
| 看容器開機次數/crash-loop | `cat data/<room_id>/logs/container-boot.log` |
| 看 gateway 啟動/退出事件 | `cat data/<room_id>/logs/gateway-exit-diag.log` |
| 看 router 自己的 log（容器化部署） | `docker compose logs -f webhook_router` |
| 手動送一則測試訊息打整條路 | `uv run python scripts/test_webhook.py --user-id <room_id> --text "..."` |
| 列出所有正在跑的 hermes 容器 | `docker ps --filter name=hermes_` |
| 手動起單一 MCP server 測試 | `docker exec -it hermes_<room_id> node /opt/data/mcp/<name>/server.mjs` |

## 4. Production 展望

每房間的檔案 log（`agent.log`／`gateway.log`／`errors.log`／`mcp-stderr.log`／
`state.db` 等）已經因為 `HOST_DATA_DIR` 的 bind mount 集中在 host 檔案系統上
了——production 只要把 `HOST_DATA_DIR` 放到一個有備份、有容量的位置（例如掛載
的資料碟），這部分**不需要**額外的集中化基礎建設，本來就不會散落。

真正「散落在多個容器」的只剩下每個容器的**stdout**（`docker logs` 看的那份）。
現階段（單機部署、房間數量有限）用 docker 內建的 `json-file` log driver
＋ `docker logs`／`docker compose logs` 已經夠用，不需要為了假設中的規模先建
Loki/Promtail 或雲端 log 服務（CloudWatch、Datadog 等）這類基礎設施——等到真的
上到多主機或單機已經多到肉眼查不過來時，再依實際痛點加一層集中收集，現在加只
是提早付維運成本。
