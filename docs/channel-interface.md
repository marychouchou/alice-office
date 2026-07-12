# 訊息管道介面（Channel Interface）

設計與實作說明：把「訊息怎麼進出這個 router」從 LINE 專屬程式碼中抽象出來，
讓 LINE、Telegram、自訂 TUI、之後的 mobile app 都能以同一套契約接上同一條
處理管線。2026-07-12 實作，已接上兩個管道：LINE（行為與抽象前完全一致）與
local dev channel（開發用，不經過 LINE 直接跟房間的 agent 對話）。

相關文件：`docs/line-hermes-message-flow.md`（LINE 端到端流程）、
`docs/router-hermes-agent-protocol.md`（router ↔ Hermes container 協定）。

## 動機

抽象前，LINE webhook 是訊息進出的唯一管道，且「管道無關」的邏輯（Google OAuth
gate、container 取得／建立、呼叫 Hermes agent）跟「LINE 專屬」的邏輯（驗簽、
去重、reply token、bubble 分段）全部混在 `router.py`。這造成兩個問題：

1. **開發不便**：想跟某個房間的 agent 說一句話，必須真的從 LINE 手機 app 發訊息，
   還得有公網可達的 webhook URL。
2. **無法擴展**：接 Telegram 或 mobile app 等於把 `router.py` 再複製改寫一次，
   gate／container／agent 的邏輯會長出第二份。

## 架構總覽

```
LINE Platform ──POST /webhook──▶ channels/line.py ──┐
                                                     │   InboundMessage
TUI / curl / ──POST /channels/──▶ channels/local.py ─┤        +
mobile app      local/messages                       │   Responder
                                                     ▼
(未來) Telegram ─POST /channels/─▶ channels/telegram.py──▶ channels/pipeline.py
                 telegram/webhook                             │
                                          gate → container → agent → send_reply
```

- **Adapter（`channels/<name>.py`）**：一個管道一個模組。負責把平台的 wire format
  轉成 `InboundMessage`，並提供一個綁定該房間的 `Responder` 負責送信。所有平台
  SDK、驗簽、去重、格式化、長度限制都關在 adapter 裡。
- **Pipeline（`channels/pipeline.py`）**：管道無關的處理管線，所有管道共用。
  不 import 任何 adapter、不碰任何平台 SDK。新增管道時**不需要動這個檔案**。
- **契約（`channels/base.py`）**：adapter 和 pipeline 之間唯一的介面。

## 契約（`channels/base.py`）

### `InboundMessage`

一則已解析成純文字、準備進管線的使用者訊息（frozen dataclass）：

| 欄位 | 意義 |
|---|---|
| `channel` | 產生這則訊息的 adapter 名稱（`"line"`、`"local"`、…），目前只用於 log |
| `room_id` | 全域唯一的聊天室 id，同時決定 container、`data/<room_id>/`、Hermes session |
| `text` | 要轉給 agent 的最終文字。媒體已由 adapter 下載落地並替換成通知文字 |

媒體處理是 adapter 的責任：管線只看得到文字。LINE adapter 透過
`line_events.resolve_inbound_text` 把 image/audio/video/file 下載到
`data/<room_id>/incoming/` 再換成通知文字；未來的 Telegram adapter 自己下載
Telegram 的檔案、落地到同一個位置即可（第二個管道需要落地邏輯時，把「寫進
incoming/ + 產生通知文字」抽成共用 helper——Rule of Three）。

### `Responder`（Protocol）

綁定「觸發訊息那個房間」的送信介面，兩個方法：

| 方法 | 語意 | LINE 實作 | local 實作 |
|---|---|---|---|
| `send_reply(text)` | 回覆觸發訊息本身 | reply token 優先、被拒 fallback Push；token 單次使用 | 收集進 HTTP response |
| `send_notice(text)` | 對同一房間送額外的旁支訊息 | 一律 Push（保住 reply token 給正式回覆用） | 收集進 HTTP response |

`send_notice` 存在的原因：Google OAuth gate 的「授權即將過期」通知必須在 agent
回覆**之前**送出，又不能把該管道「回覆這則訊息」的機制（LINE 的 reply token）
用掉。格式化（LINE 的 Markdown 去除＋bubble 分段）是 Responder 內部的事——
pipeline 給的是 agent 的原始文字，local channel 因此能拿到未破壞的 Markdown
讓 TUI／mobile 自己渲染。

### `PipelineOutcome`

`process_inbound` 的回傳值，讓同步管道能回報結果（LINE 的 background task 直接
忽略它）：`replied`／`blocked`（OAuth gate 擋下，授權連結已送）／`dropped`
（room_id 不合法）／`container_error`／`agent_error`／`delivery_error`。
管線本身**永不 raise**——LINE 是在回完 200 OK 之後的 background task 裡跑的，
所有失敗都記 log 並反映在 outcome。

### room_id 契約

`room_id` 同時是 Docker container 名稱後綴（`hermes_<room_id>`）和資料夾名稱
（`data/<room_id>/`），所以必須滿足 `is_safe_room_id`：

```
^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$
```

（Docker 名稱字元集的子集、不含路徑分隔符與 `.`、最長 64 字元。）

命名空間規則：

- **LINE**：直接用原生 id（`U`/`C`/`R` + 32 hex，33 字元，天然符合且全域唯一）。
  不加前綴是為了向後相容——既有的 `data/<room_id>/` 和容器都是用原生 id 建的。
- **local**：呼叫端自選，建議 `local_` 前綴（如 `local_dev_mary`）開沙盒房間；
  也可以**故意**填一個既有 LINE 房間的 id，直接跟那個房間的 agent／session 對話
  （debug 利器，也是不強制前綴的原因）。
- **未來的管道**：原生 id 不符合字元集或可能撞名時，adapter 必須自己正規化＋加
  前綴。例如 Telegram 的 chat id 可能是負數（supergroup 是 `-100…`），應轉成
  `tg_100…`／`tg_n100…` 這類形式。

防線有兩層：local channel 在 request model 就擋（422），pipeline 內再檢查一次
（防未來哪個 adapter 忘記正規化，不合法就 `dropped`，永遠到不了 container 層）。

## 已實作的管道

### LINE（`channels/line.py`）

行為與抽象前完全一致，只是搬家＋換介面：

- `POST /webhook` 路徑、驗簽（400）、去重、envelope 檢查全部不變。
- 舊 `router.py::_deliver_reply` 的 reply-token-first + Push fallback 變成
  `LineResponder.send_reply`；token 改為明確單次使用（用過即清空）。
- LINE wire format 仍住在 `line_*.py`（`line_events`／`line_client`／
  `line_format`／`line_verify`／`line_dedup`），adapter 只做接線——符合
  CLAUDE.md 路由表的分工。

### local dev channel（`channels/local.py`）

給 TUI／mobile 雛形／curl 用的同步 HTTP 管道：

- **端點**：`POST /channels/local/messages`，body `{"room_id": "...", "text": "..."}`。
- **回應**：`{"status": "<PipelineOutcome>", "messages": ["..."]}`——管線送出的
  所有訊息依序收集（gate 通知在前、agent 回覆在後）。status 非 `replied`／
  `blocked` 且 messages 為空時，細節在 router log。
- **認證**：`Authorization: Bearer <LOCAL_CHANNEL_TOKEN>`。`.env` 沒設
  `LOCAL_CHANNEL_TOKEN`（預設）＝整個端點停用（403）；token 錯誤回 401。
  比對用 `secrets.compare_digest`（timing-safe）。
- **同步**：response 會等 agent 回完才回來。第一次跟某房間說話還要等 container
  開機，client 請設寬鬆的 read timeout（TUI 用 300 秒）。

用法：

```bash
# .env 設 LOCAL_CHANNEL_TOKEN=<隨機字串> 並重啟 router 後：

# curl
curl -sS http://localhost:8000/channels/local/messages \
  -H "Authorization: Bearer $LOCAL_CHANNEL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room_id": "local_dev", "text": "你好"}'

# 互動式 TUI（token 自動從 --token / 環境變數 / .env 解析）
uv run python scripts/chat_tui.py --room local_dev
```

## 新增一個管道的 checklist

以 Telegram 為例（未實作，僅示意）：

1. **建 `channels/telegram.py`**，開自己的 `APIRouter`（如
   `POST /channels/telegram/webhook`），在 `main.py` `include_router`。
2. **驗證來源**：Telegram 用 `setWebhook` 時指定的 `secret_token`，比對
   `X-Telegram-Bot-Api-Secret-Token` header（等價於 LINE 的驗簽——每個管道自己
   的機制自己實作，失敗回 4xx 不得靜默）。
3. **去重**：Telegram 會重送 update，用 `update_id` 配 `EventDeduplicator`
   （`line_dedup.py` 的實作本身是通用的，只吃字串 id；第三個管道也要用時把它
   改名搬進 `channels/`）。
4. **正規化 room_id**：`chat.id` 加前綴＋處理負號（見上方 room_id 契約），
   確保過 `is_safe_room_id`。
5. **解析 inbound**：文字直接用；照片／文件下載到 `data/<room_id>/incoming/`
   換成通知文字（此時已是第二份落地邏輯，依 Rule of Three 留互相指向的註解，
   或直接抽共用 helper）。
6. **實作 Responder**：`send_reply`＝`sendMessage`（Telegram 上限 4096 字元，
   分段邏輯自己帶，比照 `line_format.split_for_line` 的做法）；`send_notice`
   也是 `sendMessage`（Telegram 沒有 reply token 概念，兩者相同）。
7. **組裝**：`InboundMessage(channel="telegram", room_id=..., text=...)` +
   Responder 丟給 `process_inbound`（webhook 管道用 background task；同步管道
   直接 await）。
8. **設定**：token 等新增到 `config.py` Settings ＋ `.env.example`。
9. **測試**：比照 `tests/test_channel_line.py`（adapter 行為）；pipeline 已有
   自己的測試，不用重測 gate／container／agent 邏輯。

**不需要做的事**：動 `pipeline.py`、動 `base.py`、動 Google gate、動
`container_manager`／`hermes_client`。如果發現新管道需要改 pipeline，先回頭
檢查是不是把管道專屬的事放錯層了。

mobile app 的路徑：雛形期直接打 local channel（帶 token）；等需要帳號綁定、
push notification 等能力時再開一個 `channels/mobile.py`，屆時 Responder 的
`send_notice` 就有自然的落點（推播）。

## 程式碼位置對照（舊 → 新）

抽象前的 `router.py` 已移除，舊文件（`docs/line-hermes-message-flow.md` 等）
提到的位置對照如下：

| 舊位置（`router.py`） | 新位置 |
|---|---|
| `line_webhook`（`POST /webhook`） | `channels/line.py::line_webhook`（路徑不變） |
| `_dispatch_event` | `channels/line.py::_dispatch_event` |
| `_deliver_reply` | `channels/line.py::LineResponder.send_reply` |
| `_process_and_reply` | `channels/pipeline.py::process_inbound` |
| `_apply_google_gate` | `channels/pipeline.py::_apply_google_gate` |
| module-level `_dedup` | `channels/line.py`（不變，仍是 process-local） |
| （更早的）`_resolve_inbound_text`／`_download_and_note_media` | 2026-07-12 稍早已搬進 `line_events.py`，本次未動 |

測試對照：`test_router.py` → `test_channel_line.py`（webhook／dispatch／
LineResponder）＋ `test_channel_pipeline.py`（管線與 gate）＋
`test_channel_local.py`（local channel）。

## 已知限制

- **`get_or_create_container` 是同步阻塞呼叫**：container 冷啟動期間會卡住整個
  event loop。這是抽象前就存在的行為（LINE 的 background task 同樣在 loop 上跑），
  local channel 只是讓它更容易被觀察到（第一句話會等很久）。要修的話是把它包進
  thread executor，屬 pipeline 層的獨立改動，與管道介面無關。
- **local channel 是同步等待**，不適合直接當 production mobile 後端——那是
  未來 `channels/mobile.py` 的事。
- **去重是 process-local**（跟抽象前相同），多 worker 部署時要換共用儲存。
- **一個房間仍然只有一個 Hermes session**：不同管道打同一個 `room_id` 會共享
  對話歷史。這是刻意的（local channel 拿來 debug 既有房間就是靠這點），但表示
  「同一個使用者在 LINE 和 app 各有一個隔離對話」需要用不同 room_id 表達。
