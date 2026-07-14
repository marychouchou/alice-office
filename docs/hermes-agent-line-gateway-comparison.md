# alice-office-router vs. Hermes Agent 內建 LINE Gateway

比較對象：本專案（`alice-office-router`，見 `src/alice_office_router/`）
與 [nousresearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 自帶的 LINE platform adapter
（文件：`hermes-agent.nousresearch.com/docs/user-guide/messaging`）。

## 摘要 (TL;DR)

本專案**完全不使用** Hermes Agent 內建的 LINE gateway/adapter。Hermes container 只透過它的
`api_server` platform（OpenAI-compatible `/v1/chat/completions`）被動收文字、吐文字；LINE 的簽章驗證、
webhook 接收、Push 回覆全部由 router 自己處理（見 `docs/hermes-agent-real-integration.md`）。

Hermes Agent 原生也有一套功能完整的 LINE 整合（gateway 內建的 20 個 platform adapter 之一），但它的
隔離模型是**「一個 profile = 一個 LINE 帳號 + 一份共用大腦，內部用 allowlist 分流多個使用者」**，跟本
專案「單一 LINE 帳號、每個聊天室各自一個硬隔離 container」的多租戶需求（見 `CLAUDE.local.md`）不相容，
所以 router 選擇繞過它、自己重做一層 LINE 進出。

## 對照表

| 面向 | alice-office-router（本專案） | Hermes Agent 內建 LINE Gateway |
|---|---|---|
| 誰接收 LINE Webhook | Router（FastAPI `POST /webhooks/line`，保留 `/webhook` alias，`channels/line/adapter.py`） | Hermes gateway 自己起一個 HTTP server（預設 `LINE_PORT=8646`，path `/line/webhook`） |
| 簽章驗證 (`x-line-signature`) | Router 自己算 HMAC-SHA256 比對，失敗回 400（`channels/line/verify.py`） | Hermes adapter 內部用 `LINE_CHANNEL_SECRET` 驗證 |
| 誰送出回覆 | Router：reply token 優先（免費、單次、~60 秒內），過期/被拒才 fallback 到 Push（`channels/line/client.py`、`channels/line/adapter.py::LineAdapter._deliver_reply`） | Hermes adapter 自己送，同樣是 reply-token-first + Push fallback |
| LINE 憑證存放位置 | 只有 router 持有 `LINE_CHANNEL_SECRET` / `LINE_CHANNEL_ACCESS_TOKEN`；Hermes container 完全拿不到（`container_manager.py` 明確不傳） | 每個 profile 自己在 config/`.env` 存一份完整憑證 |
| Router ↔ Agent 溝通協定 | `POST /v1/chat/completions`（`api_server` platform），純文字進、純文字出，帶 `X-Hermes-Session-Id: room_id`（`hermes_client.py`） | 無此層——LINE adapter 與同進程內的 `AIAgent` 直接呼叫，不透過 HTTP API |
| 多租戶隔離單位 | 每個 `room_id` → 各自獨立 Docker container + 獨立 volume（OS/容器層級硬隔離） | 每個「profile」（`hermes -p <name>`）→ 各自 `HERMES_HOME`、記憶、skills、PID；**同一 profile 內**的多個 LINE 使用者/群組/聊天室共用同一份記憶與 skills，只靠 session store 做邏輯區隔，不是硬隔離 |
| 新聊天室的處理方式 | 全自動：收到未知 `room_id` 訊息時，背景任務動態 `docker run` 建立新 container（`get_or_create_container`），無人工介入 | 手動：profile 需要人工執行 `hermes -p <name>` 建立與啟動；不會因為收到新使用者訊息就自動生出一個新 profile |
| 帳號內的使用者/群組隔離 | 天生一對一：隔離單位就是聊天室本身 | 靠 `LINE_ALLOWED_USERS` / `LINE_ALLOWED_GROUPS` / `LINE_ALLOWED_ROOMS` allowlist 決定「誰能講話」，但獲准對象全部打進**同一個** agent 大腦、同一份 skills、同一份長期記憶——這正是本專案要避開的「軟隔離」 |
| 對話記憶延續性 | Router 帶 `X-Hermes-Session-Id: room_id` 呼叫 completions API，讓同房間對話連續 | Gateway 內建 per-chat session store（SQLite + FTS5），本來就有 session 延續，含 idle（預設 1440 分鐘）/ daily（預設 4:00 AM）自動 reset 政策 |
| Sandbox / code execution 隔離 | 借用 Docker container 本身作為每個房間的沙盒邊界（一房間一容器，見 `CLAUDE.local.md` 的沙盒安全風險分析） | 靠 `terminal.backend` 設定（`local` / `docker` / `ssh` / `singularity` / `modal` / `daytona`）；**預設 `local` = 無沙盒**，跟房間/使用者身份無關，需另外手動切換 |
| 危險指令核准機制 | 未使用（router 本身不執行使用者程式碼，只轉發文字） | `approvals.mode`：manual / smart / off（`--yolo`），另有不可覆寫的 hardline blocklist（防 `rm -rf /`、fork bomb 等） |
| 未知使用者處理 | N/A（新房間直接自動建 container） | DM Pairing：陌生使用者拿到 8 碼一次性配對碼，須 bot owner 用 `hermes pairing approve` 核准後才進白名單 |
| 支援平台數 | 僅 LINE | 20 個 adapter（Telegram、Discord、Slack、WhatsApp、Signal、SMS、Email、LINE、Matrix、DingTalk、Feishu/Lark、Teams…） |
| 服務啟動方式 | Router 常駐對外；每個房間的 Hermes container 用 `gateway run` 啟動但只開 `api_server`，不啟用內建 LINE adapter | `hermes gateway` 是常駐進程，內建的所有 platform adapter（含 LINE）在同一進程內一起跑 |
| 需要對外開放的 port | Router 對外（LINE 平台打進來）；各房間 Hermes container 只掛在 Docker 內網被 router 呼叫，**不對外開放** | Hermes gateway 進程本身需要對外開放 `LINE_PORT`（預設 8646），讓 LINE 平台直接打進來（或搭配 tunnel），Media 功能還需額外 `LINE_PUBLIC_URL` |

## 多模態／檔案支援落差

實際讀了 Hermes Agent 原始碼（`plugins/platforms/line/adapter.py`、`gateway/platforms/api_server.py`）
後確認：本專案目前**完全不處理非文字訊息**，而且就算要處理，router↔container 目前唯一的溝通管道
（`api_server` 的 `/v1/chat/completions`）本身也只吃圖片，不吃檔案/語音/影片。細節如下。

### `api_server` 的多模態支援現況（`gateway/platforms/api_server.py`）

`_normalize_multimodal_content()`（`api_server.py:208-323`）明確定義了規則：

- ✅ **接受**：`image_url` / `input_image` content part，可以是 `http(s)://` 遠端 URL，也可以是
  `data:image/*;base64,...` 的 inline data URL（`api_server.py:283-292`）。
- ❌ **拒絕**：`file` / `input_file` part 一律回 400 `unsupported_content_type`，錯誤訊息直接寫明
  `"Inline image inputs are supported, but uploaded files and document inputs are not supported on
  this endpoint."`（`api_server.py:301-305`，並有專門測試 `tests/gateway/test_api_server_multimodal.py::
  test_file_part_returns_400` 驗證）。
- ❌ 沒有任何 `audio`/`video` content part 類型——`_IMAGE_PART_TYPES` 只有 `image_url`/`input_image`
  兩種，語音、影片完全沒有對應的多模態欄位可用。
- 整個 request body 上限 `MAX_REQUEST_BYTES = 10_000_000`（10 MB），會限制能塞多大的 base64 圖片。

**結論：圖片可以透過 base64 data URL 走 `/v1/chat/completions` 傳進 Hermes container；檔案、語音、
影片完全沒有對應通道，api_server 這層 API 設計上就直接拒絕。**

### Hermes 內建 LINE adapter 怎麼處理媒體（`plugins/platforms/line/adapter.py`）

| 方向 | 訊息類型 | 內建 adapter 的做法 |
|---|---|---|
| 進站（LINE→Agent） | image / audio / video / file | `_handle_message_event`（`adapter.py:932-994`）呼叫 `_download_media`（`adapter.py:1055-1073`），用 LINE Content API（`GET https://api-data.line.me/v2/bot/message/{id}/content`，`_LineClient.fetch_content`，`adapter.py:505-514`）把 binary 抓下來，`cache_image_from_bytes()` 存成本機暫存檔，把**本機檔案路徑**掛在 `MessageEvent.media_urls`／`media_types` 上餵給 agent——agent 是靠自己的檔案/視覺工具去讀這個路徑，不是靠 API 把 bytes 編碼進 prompt |
| 進站 | sticker / location | 轉成純文字佔位字串（如 `[sticker: xxx]`、`[location: 標題 地址]`），不下載任何檔案 |
| 出站（Agent→LINE） | image / audio / video | `send_image_file` / `send_voice` / `send_video`（`adapter.py:1320-1417`）——但 **LINE Messaging API 不接受二進位上傳**，image/audio/video 訊息一定要給 LINE 平台一個公開可存取的 HTTPS URL 讓 LINE 自己去抓。adapter 因此自己內建一個檔案伺服器端點（`_handle_media`，`adapter.py:1273-1318`，path `/line/media/<token>/<filename>`），用一次性 token（`secrets.token_urlsafe(32)`）+ TTL 過期 + 白名單路徑（僅 `/tmp`、`HERMES_HOME` 底下）保護，並要求設定 `LINE_PUBLIC_URL`（tunnel 或固定網域）才能運作 |
| 限制 | — | 圖片 ≤10MB、語音/影片 ≤200MB（LINE 平台本身的限制，`LINE_IMAGE_MAX_BYTES` / `LINE_AV_MAX_BYTES`） |

### 要「完整複製」到目前 router + container 架構，需要補的東西

因為本專案的架構是「router 獨佔 LINE 進出，container 只透過 `api_server` 被動問答」，上面這些行為
沒有一個是內建的，得自己在 router 端重做一份。**Phase 1（inbound 媒體 + reply token + 送出體驗）已
實作完成**，狀態如下：

| 缺口 | 狀態 | 做法 |
|---|---|---|
| 下載 LINE 傳來的圖片/檔案/語音/影片 | ✅ 已完成 | `channels/line/client.py::download_line_content()` 用 `AsyncMessagingApiBlob.get_message_content` 下載 |
| 把媒體交給 Hermes agent | ✅ 已完成（採「共享檔案落地」而非 base64 多模態） | `channels/line/events.py::_download_and_note_media()` 把檔案寫進 `config.DATA_DIR/room_id/incoming/`（container 內對應 `CONTAINER_DATA_DIR/incoming/`，見 `container_manager.py`），送一則文字通知 agent 路徑，讓 container 內**真正的 Hermes agent** 用自己的 vision/STT/檔案工具處理——這是刻意選擇，因為每個房間本來就跑真實 Hermes agent，不必疊床架屋改走 base64 多模態 API |
| Reply token 優先、Push 為 fallback | ✅ 已完成 | `channels/line/adapter.py::LineAdapter._deliver_reply()`：有 `replyToken` 就先 reply，被 LINE 拒絕（過期/已用）才 fallback push；不做本地 TTL 預判，交給 LINE API 自己的回應驅動 |
| 長文字自動分段 | ✅ 已完成 | `channels/line/format.py::split_for_line()`，1:1 port `adapter.py:212-257` 的演算法 |
| Markdown 去除 | ✅ 已完成 | `channels/line/format.py::strip_markdown_preserving_urls()`，1:1 port `adapter.py:174-211` |
| Webhook 事件去重 | ✅ 已完成 | `channels/line/dedup.py::EventDeduplicator`，1:1 port `adapter.py:373-390`（用 `webhookEventId`） |
| 貼圖/位置轉文字 | ✅ 已完成 | `channels/line/events.py::resolve_inbound_text()` 轉成 `[使用者傳送了貼圖：...]` / `[使用者傳送了位置：...]` 佔位文字 |
| Agent 產生的圖片/檔案送回 LINE（outbound 媒體） | ⏳ 未做（Phase 2） | 需要新協定（agent 寫檔到 volume + marker）+ router 自建簽名 token 檔案伺服端點 + router 本身要有公開 HTTPS URL；見 `~/.claude/plans/optimized-munching-glade.md` |
| Slow-LLM postback 按鈕 | ⏳ 未做（Phase 2） | 需要 pending 狀態快取 + `postback` 事件處理，port `adapter.py` 的 `State`/`RequestCache`/`_keep_typing` |

多事件批次處理與 `events[0]` 限制也一併修掉了：原本 `channels/line/adapter.py` 只處理 webhook body 的第一個
event，現在會 loop 過整個 `events` 陣列（`_dispatch_event`），每個 event 各自去重、解析、排背景任務。

## 為什麼不用 Hermes 內建的 LINE Gateway

1. **多租戶模型不同**：Hermes 的「profile」是最小隔離單位，一個 profile 對應一組完整憑證與一份共用
   記憶/skills；多個使用者共用同一 profile 時彼此**沒有硬隔離**，只靠 session 區分對話串。而本專案的
   需求是「單一 LINE 帳號，但每個聊天室（room/group/user）都要像有自己專屬 Agent，彼此完全讀不到對方
   的檔案與記憶」——這等於要求「一個帳號、N 個 profile」，但 Hermes 沒有「同一 LINE 帳號自動路由到不同
   profile」的機制。
2. **profile 建立是手動的**：Hermes 沒有「收到新聊天室訊息 → 自動生一個新 profile/container」的動態
   擴展能力，這正是 `CLAUDE.local.md` 裡明確要求的「動態自動化擴展」核心需求。
3. **Webhook 責任重複**：若每個房間都各自跑一份帶內建 LINE adapter 的 Hermes gateway，會有 N 個進程
   同時嘗試用同一組 `LINE_CHANNEL_ACCESS_TOKEN` 對外處理 webhook / push，彼此衝突（Hermes 文件本身也
   提到「already in use by another profile」的錯誤情境）。改成「router 統一收 webhook、hermes 純被動
   當大腦」後，對外只有一個入口，職責單純。

## 相關文件

- `docs/hermes-agent-real-integration.md` — 從 mock 換成真實 Hermes Agent 的變更紀錄與架構決策過程
- `docs/line-hermes-message-flow.md` — 單則訊息從 LINE 到 Hermes container 的完整流程細節
- `CLAUDE.local.md` — 本機端隔離與自動化架構設計書（本專案需求源頭）
- Hermes Agent 官方文件：`hermes-agent.nousresearch.com/docs/user-guide/messaging`（Messaging Gateway
  總覽）與其 LINE 專頁、`docs/developer-guide/architecture`（profile / gateway 架構）、
  `docs/user-guide/security`（terminal backend / 核准機制 / DM pairing）
- Hermes Agent 原始碼（`github.com/NousResearch/hermes-agent`，直接讀 source 確認的部分）：
  `plugins/platforms/line/adapter.py`（LINE adapter 完整實作）、
  `gateway/platforms/api_server.py`（`api_server` platform，含多模態正規化邏輯）、
  `tests/gateway/test_api_server_multimodal.py`（多模態行為的測試佐證）、
  `website/docs/user-guide/messaging/line.md`（LINE 官方設定文件）
