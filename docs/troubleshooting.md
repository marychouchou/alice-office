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
| Router 有沒有收到這個 webhook | Router 自己的 log（host 模式＝terminal 直接印；container 模式＝`docker compose logs`） | `docker compose logs -f webhook_router`（或本機開發的 `uv run fastapi dev` terminal），找 `"event":"http_request"` 且 `"path":"/webhook"` 的那行看 `status` |
| 房間 container 活著沒、activty 狀態 | `docker ps` | `docker ps -a --filter name=hermes_<room_id>` |
| Hermes agent（api_server）有沒有收到這次請求 | `docker logs`，或房間自己的 `agent.log` | `docker logs --tail 50 hermes_<room_id>`；或 `tail data/<room_id>/logs/agent.log`，找 `aiohttp.access: ... "POST /v1/chat/completions HTTP/1.1" 200` |
| agent 這次用了什麼工具、耗時、輸出大小 | `agent.log` 的 `agent.tool_executor` 行 | `grep tool_executor data/<room_id>/logs/agent.log` |
| agent 這次工具呼叫的**完整參數與回傳內容** | `state.db`（sqlite） | 見 2.2 節的 sqlite 指令 |
| MCP server 為什麼掛掉/沒註冊成功 | `mcp-stderr.log`、`agent.log` 的 `tools.mcp_tool` 行 | `tail -n 50 data/<room_id>/logs/mcp-stderr.log` |
| 容器為什麼起不來 / health check timeout | `container-boot.log`、`gateway-exit-diag.log`、`docker logs` | 見 2.4 節 |
| Google OAuth 卡在哪一步 | 房間的 `google/` 目錄、router log 的 oauth 錯誤行 | 見 2.5 節 |
| router 自己有沒有丟例外（容器編排／呼叫 agent／回推 LINE 失敗） | router 自己的 log | 見下方「Router 自己會記錄的行為」 |
| **以上三種來源用同一個 `room_id` 串起來一次看**（選配，要先啟用 log 堆疊） | Grafana → Explore → Loki | 見下方「集中式查詢（選配）」 |

### Router 自己會記錄的行為

`main.py` 呼叫 `logging_setup.configure_logging()`（見 `docs/logging-design.md`），
沒有另外設檔案 handler，所以 router 的 log **就是它的 process 標準輸出**：host 模式
是 terminal，container 模式是 `docker compose logs webhook_router`（`json-file`
log driver 已經在幫你把它寫進磁碟，見第 4 節）。預設 `LOG_FORMAT=json`，一行一個
JSON 物件（`docker compose logs --no-log-prefix webhook_router | jq .`），每行自帶 `request_id`、
`room_key`、`event_id`、`channel`、`container` 等欄位，所以可以直接用
`jq 'select(.room_key=="line_U1234")'` 把單一房間的行挑出來；本機開發設
`LOG_FORMAT=console` 會變成彩色好讀的格式。目前 router（`channels/line/adapter.py`、
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
`Waiting for Hermes agent at ... to become ready...`（只在容器真的還沒就緒時出現）、
`Docker API error for container ...`。

gate 擋下時的背景暖機是兩步（容器 → agent 探針 → 刪掉探針 session，設計見
`docs/router-hermes-agent-protocol.md`「暖機探針」），`core.py` 對應的行是：

- `Container warm for room ...`（INFO）：第 1 步成功，容器起來且 `/health` 通過。
- `Container warm-up failed for room ...`（ERROR）：第 1 步失敗，**不會**再做探針；
  使用者不會收到通知，下一則訊息走正常路徑再試。
- `Agent warm for room ...（N ms）`（INFO）：第 2 步成功，這個房間之後的第一則真實
  訊息不用再付 Hermes 每進程一次的 ~4.5 秒 tool registry 探測。
- `Agent warm-up probe failed for room ...`（WARNING）：探針失敗，只代表使用者的第一輪
  要自己付那 4.5 秒；房間不算已探測，下一則被擋的訊息會再試一次。
- `Agent warm-up failed for room ...`（ERROR）：探針丟出預期外的例外型別（不是
  HTTP／逾時／`ValueError`），要當成 bug 看。
- `Could not delete warm-up session for room ...`（WARNING）：探針的 session 沒刪成功，
  該房間的 `session_search` 可能會搜到一輪 `warmup-probe` 的對話；手動善後
  `curl -X DELETE -H "Authorization: Bearer $HERMES_API_SERVER_KEY"
  http://<container>:8642/api/sessions/warmup-probe`。
- 刪除成功則由 `hermes_client` 記結構化事件 `hermes_session_deleted`
  （帶 `session_id`／`status`）。

**預期現象**：房間的 `data/<room_id>/logs/agent.log` 在容器剛起來時會有一輪
`session=warmup-probe` 的對話（一句「回 OK 就好」），這是暖機探針，不是使用者的訊息，
也不會留在 `state.db`（跑完就被刪了）。

注意：`line_webhook` 本身在簽章驗證通過、events 解析完之後**沒有**額外印一行
「收到 webhook」——訊號是每個 request 一行的 `"event":"http_request"`（帶
`method`／`path`／`status`／`duration_ms`），由 `logging_setup` 的 middleware 印，
不是 uvicorn 的 access log（那份已關閉，避免兩種格式混在同一個 stdout）。

### 集中式查詢（選配：Grafana + Loki）

上面那張表是「一次看一個地方」的原始路徑，永遠可用、不依賴任何額外服務。房間一多，
或是要跨 router／容器 stdout／檔案 log 三種來源追同一則訊息時，可以另外啟用
`deploy/logging/` 的堆疊（Alloy 收集 → Loki 儲存 → Grafana 查詢，預設不開）：

```bash
# .env 先設好 GRAFANA_ADMIN_PASSWORD，然後在 repo 根目錄
docker compose -f docker-compose.yml -f deploy/logging/docker-compose.logging.yml up -d
# 或 ./scripts/deploy_host.sh --with-logging
#
# Grafana 只綁 127.0.0.1:3000，從自己的機器開 tunnel 再用瀏覽器連：
ssh -N -L 3000:127.0.0.1:3000 <user>@<host>   # → http://localhost:3000（帳號 admin）
```

Loki 資料來源已經由 provisioning 自動接好，進 Grafana 直接按 **Explore**。
Label 只有六個——`service`（`router`／`agent`／`infra`）、`room_id`、`container`、
`source`（`docker`／`file`）、`file`（`agent.log`／`errors.log`…）、`level`（只有
router 的 JSON 行有）；`request_id`、`event_id`、`sender_id` 一律留在行內，
用 `| json` 過濾（設計理由見 `docs/logging-design.md` §5.4）。常用查詢：

```logql
# 一個房間橫跨三種來源（router JSON 行 + 容器 stdout + logs/*.log）的全部紀錄
{service=~"router|agent"} | json | room_key="<room_id>" or room_id="<room_id>"

# 只要該房間 agent 那兩種來源（最快，不做 JSON 解析）
{room_id="<room_id>"}

# 某次 webhook 的完整路徑
{service="router"} | json | request_id="<request_id>"

# 所有房間的 agent error
{service="agent", file="errors.log"}

# router 的例外與 5xx
{service="router", level="error"}

# 某房間每一輪的結果狀態與耗時（對話內容不在 Loki，用 scripts/conversations.py 看）
{service="router"} | json | event="conversation_turn" | room_key="<room_id>"

# 全部房間裡 agent 失敗的那一輪（envelope 含 session_id，可對回 state.db）
{service="router"} | json | event="conversation_turn" | outcome="agent_failed"

# log 堆疊自己壞掉時（Alloy／Loki／Grafana 的 stdout 也有收）
{service="infra"}
```

兩個會浪費時間的點：

- **`{a} or {b}` 不是合法的 LogQL**——`or` 只能接在 label filter 後面，不能把兩個
  stream selector 聯集起來（`parse error: unexpected type for left leg of binary
  operation (or)`）。所以跨來源要寫成上面第一條那種「先用 `=~` 把 service 選起來，
  再用 `| json | A or B` 過濾」的形狀。
- **host 模式（`ROUTER_IN_DOCKER=false`）的 router 不會進 Loki**：Alloy 靠 Docker
  label 找容器，host process 的 stdout 它看不到。開發時看 terminal 就好
  （`LOG_FORMAT=console`）；`{service="router"}` 只有容器化部署才有東西。

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
   `Hermes agent request failed for room`／`Hermes agent request timed out for room`
   （逾時見 2.10 節）。
6. 有收到請求但 agent 側報錯：看 `data/<room_id>/logs/errors.log`（WARNING 以上）
   跟 `agent.log` 該次 `session=` 附近的行——`agent.conversation_loop` 會記錄
   `API call #N` 與 `Turn ended`，`Turn ended` 沒出現代表這次 turn 卡住或還在跑。
7. 確認回覆真的送回 LINE：router log 找 `LINE reply token rejected ... falling
   back to push`（正常）或 `Failed to push LINE reply for room`（真的失敗，通常是
   `LINE_CHANNEL_ACCESS_TOKEN` 或假的 room_id）。

> 1:1 聊天會顯示 LINE 載入動畫直到回覆送出，群組沒有；動畫失敗只記 warning
> （`Failed to show LINE loading animation for room`）不影響回覆。

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

**容器的 label 與 log 上限**：router 建立房間容器時會帶上
`alice.role=agent`／`alice.room_id=<room_id>`（room key 的前綴已經帶著 channel，所以沒有第三個 label），並把
stdout 限制成 `json-file` 的 10m × 3（見 `docs/logging-design.md` §5.2）。這兩個
都是**建立時**才決定的屬性：2026-09 之前建立的既有房間容器沒有 label、也沒有大小
上限，重啟或升級 image 都不會補上——要讓它們生效只能砍掉讓 router 重建：

```bash
docker rm -f hermes_<room_id>        # 下一則訊息進來時 router 會自動重建
docker inspect hermes_<room_id> | jq '.[0].Config.Labels, .[0].HostConfig.LogConfig'
```

砍容器不會動到 `data/<room_id>/`（對話、skills、config.yaml 都在 bind mount 上），
只會中斷一次、下一則訊息要等容器重新開機。

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

### 2.6 改了 config.yaml / skills / MCP / SOUL.md 沒生效

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
- `SOUL.md`（人設）：房間自己的 `data/<room_id>/SOUL.md` 是 write-once seed（來源
  `src/hermes/SOUL.md`），也**不**在 `dev_sync_src.py` 的同步範圍內（它是使用者可能
  已經客製化過的人設，不該被開發腳本強制蓋掉）。改完 repo 樣板只影響之後新建立的
  房間；既有房間要套用新版，手動
  `cp src/hermes/SOUL.md data/<room_id>/SOUL.md && docker restart hermes_<room_id>`。

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

### 2.8 一鍵 e2e smoke test（整條管線的可重複驗收）

`scripts/e2e_smoke.py` 把 2.7 的手動 curl 驗證固化成**一個指令**：它會自己起一個
拋棄式的 uvicorn（另開一個 port，用進程環境變數注入 `API_CHANNEL_TOKEN` 並設
`GOOGLE_OAUTH_GATE=false`，**不改動 `.env`**），依序打 API 通道的授權／驗證與一次
真實 happy path（router → core → container_manager → 真容器 → 真 LLM → 回覆），
最後把自己建立的 container／`data/` 資料夾清乾淨。

```bash
uv run python scripts/e2e_smoke.py            # 只跑 API 通道（拋棄式房間 api_e2e）
uv run python scripts/e2e_smoke.py --line      # 另外送一則合法簽章的 LINE webhook
uv run python scripts/e2e_smoke.py --keep       # 保留 container / data 供事後檢查
uv run python scripts/e2e_smoke.py --port 8901  # 預設 8899 被占用時換 port
```

每一項檢查印成一行 numbered PASS/FAIL，全過退出碼 `0`、任一失敗 `1`。它會驗：無
Authorization → `401`、錯 bearer → `401`、壞 `room_key` → `422`、空白 `text` →
`422`、happy path → `200` 且 `replies` 非空、容器 `hermes_api_e2e` 存在且
`data/api_e2e/` 已 seed。`--line` 另外驗 `POST /webhooks/line` 合法簽章回 `200`
（但 outbound 送達**無法**驗證——用的是假 replyToken，router 的 push fallback 會對
真實 LINE API 失敗並記 log，屬預期）。前置條件同 2.7：Docker 可用、`HERMES_IMAGE`
已 build、`.env` 已設成 host 模式。失敗時 router 的完整輸出會落在腳本印出的
`router log →` 路徑，可據此排查。

### 2.9 Agent 說「無法讀取 PDF」／`pymupdf 未安裝`／`OCR 服務連線失敗`

容器裡有三個 Python（見 AGENTS.md「Hermes Container Model」）：Hermes 本體的
`/opt/hermes/.venv`、官方 bundled skill 用的 `/opt/skills/.venv`（terminal 裡的
`python`／`pip`）、我們自家工具用的 `/opt/tools/.venv`（`tools-python`）。症狀來源
（2026-09-14 Oregon 實例，舊 image `v1`／`v2`）：當時還沒有 `/opt/skills/.venv`，terminal
的 `python` 是沒有 pip 的系統 Python，agent 照官方 `ocr-and-documents` skill 跑
`python scripts/extract_pymupdf.py` → `ModuleNotFoundError`；想 `pip install` 又被安全掃描
卡成 `pending_approval`（api_server 模式沒有人能核准，見 `approvals.mode`）。

`v3` 起 image 內建 `/opt/skills/.venv`（預裝 `skills-requirements.txt`，含 pymupdf）與
`alice/runtime-env` skill（Hermes 開機 sync 進每個房間）。排查：

```bash
# 1. 房間用的 image 有沒有這兩樣（v3 之前的 image 都沒有）
docker exec hermes_<room_id> bash -lc 'command -v python pip; python -c "import pymupdf, sys; print(sys.prefix)"; ls /opt/hermes/skills/alice/runtime-env'
#    預期：/opt/skills/.venv/bin/python、/opt/skills/.venv/bin/pip、印出 /opt/skills/.venv
# 2. 房間副本有沒有 sync 到（Hermes 開機 manifest sync；房間手改過的 skill 會被跳過，屬預期）
docker exec hermes_<room_id> ls /opt/data/skills/alice/runtime-env
# 3. 直接驗證抽文字這條路本身是通的（跟官方 skill 文件一樣用 python）
docker exec hermes_<room_id> bash -lc 'python /opt/data/skills/productivity/ocr-and-documents/scripts/extract_pymupdf.py "/opt/data/incoming/<檔名>.pdf" --pages 0'
```

沒有 1 → bump `HERMES_IMAGE` 到 v3 以上，`docker rm -f hermes_<room_id>` 讓房間用新
image 重建（`data/<room_id>/` 不動）。有 1 沒 2 → `docker restart` 觸發一次 sync。
官方 skill 要的套件不在預裝清單 → agent 會自己 `pip install`（容器本地、重建消失）；
常用的就加進 `skills-requirements.txt` 重 build。**自家** plugin 的套件缺了是另一個環境的事，
看 `src/hermes/runtime/pyproject.toml` 與 `tools-python -c "import …"`。

PDF／圖片一律靠 `local-tools` 的 `image_ocr` 工具呼叫，**沒有自動注入**（2026-09-15 移除
`pre_llm_call` hook：hook 注入的內容不會進 session 持久化，下一輪就消失，模型卻會憑印象
編造內容）——所以「agent 說讀不到檔案」要先確認它真的呼叫了 `image_ocr`（見 2.2 節的
`tool_executor` log）。

### 2.10 使用者說沒收到回覆，log 是 `agent_failed` / `ReadTimeout` / `TimeoutError`

`agent_failed` 代表這一輪沒把 agent 的答案送回房間。看 envelope 的 `error` 分類：

Router 是用 SSE streaming 呼叫 agent 的，Hermes 每靜默 30 秒會送一次 `: keepalive`，所以
逾時有兩條、意義完全不同（改完任一個都要重啟 router：`docker compose up -d webhook_router`）：

- `agent: ReadTimeout`（**idle／靜默逾時**，router log 會寫 `hit the idle timeout ... after
  120.0s`）→ 連 keepalive 都沒進來，**agent 或容器八成卡死了**，不是「算太久」。先照 2.1 節
  第 3～6 步看容器還在不在、`agent.log` 最後停在哪；真的是某類工具會長時間凍住整個進程
  才調大 `HERMES_IDLE_TIMEOUT_SECONDS`（`.env`，預設 120 秒）。
- `agent: TimeoutError`（**ceiling／絕對上限**，router log 會寫 `hit the ceiling timeout ...
  after 3600.0s`）→ agent 一路都活著（keepalive 有來），只是整輪真的跑超過一小時。正常
  不該踩到；要讓這種超長任務跑完就調大 `HERMES_REQUEST_TIMEOUT_SECONDS`（`.env`，預設
  3600 秒），也順便看看是不是 prompt 把它推進了無盡的工具迴圈。
- 兩種逾時 **agent 本身都不會被中斷**：那一輪的答案仍然寫進房間的 Hermes session，所以請
  使用者「再問一次」很便宜，模型接得上前一輪的脈絡。
- `agent: ValueError: Hermes agent failed: ...` → agent 自己回報該輪失敗（結束 chunk 的
  `hermes.failed`），冒號後面就是它給的原因；往 `data/<room_id>/logs/errors.log` 對時間找。
- 其他（`container: ...`、HTTP 4xx/5xx、`ValidationError`）→ 照 2.1 節從第 3 步往下查。

回覆被截斷（使用者說「講到一半就沒了」）不會變成 `agent_failed`：router 照送截到一半的
文字，只在 router log 記一筆 `hermes_agent_truncated`（帶 `finish_reason="length"`），那是
LLM 的 max tokens 設定問題，不是 router 的逾時。

以上每一種使用者**都會**收到一則固定提示（逾時兩種走 `core.AGENT_TIMEOUT_NOTICE`，其餘走
`AGENT_FAILURE_NOTICE`），不會是完全沒有回應；如果使用者連提示都沒收到，問題在送訊那一段，
看 2.1 節第 7 步。

同一房間的訊息會排隊（log `room_turn_queued`，欄位 `waited_ms`），上一輪跑很久時後面的
訊息會等，看起來像「沒回」其實是還沒輪到。

```bash
uv run python scripts/conversations.py stats --since 7d      # agent_failed 占比
grep -E 'ReadTimeout|TimeoutError' data/_conversations/<room_key>.jsonl | tail
docker compose logs webhook_router | grep -E 'idle timeout|ceiling timeout|hermes_agent_truncated'
```

### 2.11 使用者收到「系統暫時無法回應」，envelope error 是 `Context length exceeded`

Hermes 每次請求預設預留視窗一半當輸出（131K 視窗就是 65,536），prompt 到 65K 就撞
`ContextWindowExceededError`，而 Hermes 自動壓縮在小視窗下最低 75% 才觸發，永遠來不及。
qwen3 的思考模式會讓一次呼叫吐出數萬 reasoning token，且 Hermes 會把它回傳給同一輪之後
每次呼叫，所以幾次工具呼叫後就滿了（2026-09-15 實例：第一輪 64,873 個輸出 token，第六次
呼叫 65,537 + 65,536 > 131,072）。`src/hermes/config.template.yml` 對新房間已預設三件事：

- `model.context_length: 262144`：provider 是 LiteLLM proxy，`/model/info` 不回報視窗大小，Hermes
  對 qwen 會寫死 131,072（真實 `max_model_len` 是 262,144，用超大 `max_tokens` 打一次端點就能從
  錯誤訊息讀到）。少一半視窗的後果是每兩次呼叫就壓縮一次、原地打轉。
- `model.max_tokens: 32768`：輸出預留 32K（一次寫 13 KB Markdown 的 `write_file` 要 16K 以上），
  prompt 可長到約 230K，壓縮（197K）先觸發。
- `providers.custom.extra_body.chat_template_kwargs.enable_thinking: false`：關掉思考模式。
- `approvals.mode: smart`：被誤判的 `python -c`／`execute_code` 由輔助模型自動放行，
  不再讓 agent 為了驗算多燒三四次呼叫。

既有房間要自己改 `config.yaml`（write-once）再 `docker restart`。跨輪的保險是 router 的
`SESSION_ROTATE_PROMPT_TOKENS`（預設 120000，高於 Hermes 壓縮點；想讓 router 先換 epoch
帶交接摘要，調到 80000 左右）。該輪的 session 已塞滿時，下一句先 `/new`。

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
| 只挑某個房間的 router log 行 | `docker compose logs --no-log-prefix webhook_router \| jq 'select(.room_key=="<room_id>")'`（沒有 `--no-log-prefix` 的話每行前面會多一段 `webhook_router  \|`，jq 會直接 parse error） |
| 確認容器的 label 與 log 上限有生效 | `docker inspect hermes_<room_id> \| jq '.[0].Config.Labels, .[0].HostConfig.LogConfig'` |
| 啟用集中式 log（Alloy+Loki+Grafana，選配） | `docker compose -f docker-compose.yml -f deploy/logging/docker-compose.logging.yml up -d` |
| 關掉集中式 log（router 不受影響） | `docker compose -f docker-compose.yml -f deploy/logging/docker-compose.logging.yml stop alloy loki grafana` |
| 確認 Alloy 有在收（列出目前所有 label 值） | `docker exec grafana curl -s http://loki:3100/loki/api/v1/label/room_id/values`（Loki 只在 `logging_net` 上，房間容器連不到，一定要從 grafana／alloy 裡面打） |
| 手動起單一 MCP server 測試 | `docker exec -it hermes_<room_id> node /opt/data/mcp/<name>/server.mjs` |
| 列出所有房間的對話量與用量 | `uv run python scripts/conversations.py rooms` |
| 看某房間的逐輪對話（含結果狀態與耗時） | `uv run python scripts/conversations.py show <room_id> [--with-tools]` |
| 跨房間找「誰問過某個關鍵字」 | `uv run python scripts/conversations.py search "<關鍵字>"` |
| 把某房間匯出成 Claude Code 可 `@file` 的逐字稿 | `uv run python scripts/conversations.py export --room <room_id> --since 7d --format md --out /tmp/x` |
| outcome 分布／agent_failed 率／p95 耗時 | `uv run python scripts/conversations.py stats --since 30d` |
| 清掉太舊的 turn envelope（`_conversations/*.jsonl` 預設永不刪，這是唯一的保留期工具；先 `--dry-run`） | `uv run python scripts/conversations.py prune --older-than 90d --dry-run` |
| 產一份給人看的單房間 HTML transcript | `docker exec hermes_<room_id> hermes sessions export --session-id <session_id> --format html --yes /tmp/x.html` |
| 看某房間的 token／成本／工具使用統計 | `docker exec hermes_<room_id> hermes insights --days 7` |
| 看某個 session 的 Hermes 內部 log | `docker exec hermes_<room_id> hermes logs --session <session_id>` |

## 4. Production：什麼時候該打開集中式 log

每房間的檔案 log（`agent.log`／`gateway.log`／`errors.log`／`mcp-stderr.log`／
`state.db` 等）已經因為 `HOST_DATA_DIR` 的 bind mount 集中在 host 檔案系統上
了——production 只要把 `HOST_DATA_DIR` 放到一個有備份、有容量的位置（例如掛載
的資料碟），這部分本來就不會散落。每個容器的 **stdout** 也已經有
`json-file` driver 的 10 MB × 3 上限（router 在 `docker-compose.yml`、每個
`hermes_<room_id>` 在 `container_manager.py`），不會無限長大。

**預設維持現況**：`docker logs` / `docker compose logs` / `tail` 三條路徑，零額外
基礎設施，客戶部署不用多跑三個容器。

**`deploy/logging/` 的堆疊已經做好，但預設關閉**（`docker-compose.yml` 完全沒提到
它，只在多帶一個 `-f` 時才存在）。出現下面任一個訊號再打開，不要預先開：

| 訊號 | 為什麼集中式會解掉它 |
|---|---|
| 房間數多到「先看哪個容器」本身就要猜 | `{room_id="…"}` 一條查詢就把三種來源按時間排好 |
| 要追的問題橫跨 router 與 agent（訊息進去了但沒回） | `request_id` 一路從 webhook 帶到 agent 呼叫，`\| json \| request_id="…"` 直接拉出整條路徑 |
| 要看「上週三那次」——但 `json-file` 已經輪替掉了 | Loki 保留 30 天（`retention_period: 720h`），跟容器生命週期脫鉤：容器被 `docker rm` 重建，之前的 log 還在 |
| 要問「所有房間的 MCP 失敗率」這種跨房間問題 | LogQL 的 `sum by (room_id) (rate(...))`，`grep` 做不到 |

**代價**（本機實測，`docker stats --no-stream`）：Alloy 65 MB、Loki 107 MB、
Grafana 310 MB，合計約 480 MB RAM；Loki 磁碟在單一房間、約 500 行的情況下是
704 KB，數十房間、30 天估計數百 MB 到數 GB。三個容器都 `restart: unless-stopped`，
壞掉不影響 router（Alloy 只是讀 `docker.sock` 與 `/rooms` 的旁觀者）。

更早期的判斷是「先不建集中式 log」，那個判斷的前提（單機、小規模）沒有變，
變的是成本：收集端做成 opt-in 之後，不開就是零成本，所以不再需要「等到很痛才
開始建」——痛的時候多打一個 `-f` 就好。設計與方案比較見 `docs/logging-design.md`。
