# Channels 設計導讀（step-by-step）

> 這是一份「帶著讀」的導覽文件：跟著一則訊息從 LINE 平台走到 Hermes agent、再走回聊天室，
> 沿路把 `src/alice_office_router/channels/` 每個檔案的角色講清楚。
> 設計取捨的完整論證在 [channel-interface-design.md](channel-interface-design.md)（下稱「設計文件」），
> 本文引用其章節編號（如 §4.3）時指的就是那份文件。

---

## Step 0：先抓住一個核心觀念

整個 channels 層只在回答一個問題：

> **「怎麼讓核心流程（gate → 容器 → agent）完全不知道訊息是從哪個通訊軟體來的？」**

答案是切成兩半：

```
┌─────────────── channel 世界（每個通道一份）───────────────┐
│  驗簽、wire format 解析、dedup、媒體下載、回覆送達          │
│  channels/line/…   channels/api.py                        │
└──────────────────────────┬────────────────────────────────┘
                           │ 只透過一個型別溝通：InboundMessage
                           ▼
┌─────────────── channel-free 世界（全體共用一份）───────────┐
│  core.process_inbound：Google gate → 容器 → agent          │
│  收 InboundMessage，回傳 list[str]（要送回房間的文字）      │
└────────────────────────────────────────────────────────────┘
```

兩個不變式（違反任一個就是設計被破壞了）：

1. **core 只認 `InboundMessage`，只回 `list[str]`**。它不碰任何通道的送訊 API、
   reply token、訊息長度限制——所以它可以直接 unit test，也可以被任何 adapter 重用。
2. **每個 adapter 完全擁有自己通道的 wire format**。LINE 的簽章、事件結構、
   5000 字泡泡限制……這些知識只存在 `channels/line/` 底下，洩漏到別處就是 bug。

---

## Step 1：目錄地圖

```
channels/
├── __init__.py      # enabled_adapters()：唯一的 adapter 註冊表
├── base.py          # 共用契約：InboundMessage + ChannelAdapter Protocol
├── api.py           # 第一方 API 通道（TUI / mobile / dev 用），一個檔案就夠
└── line/            # LINE 通道，依「改動理由」拆成六個檔案
    ├── adapter.py   # 編排者：webhook 進來 → 分派 → 呼叫 core → 送回覆
    ├── events.py    # LINE wire format：pydantic 模型 + 訊息 → 文字的解析
    ├── verify.py    # HMAC-SHA256 驗簽（純函式）
    ├── dedup.py     # webhookEventId 去重（有界的 in-memory set）
    ├── client.py    # 對 LINE Messaging API 的呼叫：reply / push / 下載媒體
    └── format.py    # 出站格式化：去 Markdown + 切成 ≤5 個 ≤4500 字的泡泡
```

記法：**`adapter.py` 是唯一「懂流程」的檔案，其他五個各懂一件事**。
adapter 依序呼叫它們，自己不含任何解析或格式化邏輯。

---

## Step 2：共用契約（`base.py`）

這是整層抽象的全部——就兩個東西，總共 60 行：

### 2a. `InboundMessage` — core 唯一看得懂的輸入

```python
class InboundMessage(BaseModel):
    channel: str    # 來源 adapter 名稱，如 "line"
    room_key: str   # 全域唯一的房間鍵，core 用它路由（見 Step 5）
    text: str       # 純文字——媒體/貼圖/位置已被 adapter 換成 placeholder
```

注意 `text` 的註解：**「已被 adapter 換成 placeholder」**。這句話就是抽象的分工線——
把一張圖片變成「檔案已存到某路徑」的文字通知，是 LINE 的知識（要用 LINE 的 token 下載），
所以發生在 adapter 側；core 收到時世界上只剩純文字。

### 2b. `ChannelAdapter` — 每個通道要滿足的結構契約

```python
class ChannelAdapter(Protocol):
    name: str                            # 也是 /webhooks/{name} 的路徑段
    def api_router(self) -> APIRouter: ...
```

用的是 `typing.Protocol`（結構型別）而不是 ABC 繼承——adapter 不需要 import 或繼承任何
基底類別，只要「長得像」就算數。契約小到只有一個屬性一個方法：mount 需要知道的就這麼多，
其餘（怎麼驗簽、怎麼回覆）都是 adapter 的私事。

---

## Step 3：註冊與掛載（`channels/__init__.py` + `main.py`）

```python
# channels/__init__.py
def enabled_adapters(config: Settings) -> list[ChannelAdapter]:
    adapters: list[ChannelAdapter] = [LineAdapter()]
    if config.API_CHANNEL_TOKEN:
        adapters.append(ApiChannelAdapter())
    return adapters
```

```python
# main.py
for adapter in enabled_adapters(get_settings()):
    app.include_router(adapter.api_router(), prefix=f"/webhooks/{adapter.name}")
```

三個值得注意的決定：

- **靜態清單，不做動態發現**。沒有 plugin 掃描、沒有 entry point 註冊——新通道就是
  在這個 list 加一行（§4.2、§5 刻意不抽象）。
- **「未啟用」＝「不在清單裡」**，而不是散落各處的 `if api_enabled` 旗標。這正是
  CLAUDE.md Known Anti-Patterns 第 7 條（`google_oauth_enabled` 散落 5 處）想避免的
  反例的正面示範。
- `main.py` 還多掛了一個 legacy 別名 `/webhook`（同一個 LINE handler），因為 LINE OA
  後台目前還指向舊路徑；等後台改指 `/webhooks/line` 就可刪（`main.py:44-49`）。

---

## Step 4：跟著一則 LINE 訊息走（入站）

現在假設使用者在 LINE 傳了「幫我查一下下週行程」。LINE Platform 對
`POST /webhooks/line` 發出一個 webhook。入口是
`LineAdapter._handle_webhook`（`channels/line/adapter.py:68`），依序發生：

### 4a. 驗簽（`verify.py`）

```python
if not verify_line_signature(raw_body, signature, settings.LINE_CHANNEL_SECRET):
    raise HTTPException(status_code=400, detail="Invalid signature")
```

對 **raw body bytes** 算 HMAC-SHA256、base64 後與 `x-line-signature` header 做
**常數時間比較**（`hmac.compare_digest`，防 timing attack）。失敗回 400，不靜默忽略
（CLAUDE.md Security 條款）。注意必須用 raw bytes——先 parse 成 JSON 再 serialize 回去
簽章就對不上了，所以 handler 先 `await request.body()` 再 `request.json()`。

### 4b. 解析 envelope（`events.py` 的 `WebhookBody`）

LINE 一個 webhook POST 可能夾帶**多個 event**（batch）。解析哲學寫在 `events.py` 模組
docstring：**「對值寬鬆、對形狀嚴格」**：

- 未知的 `type` 字串照樣解析通過（LINE 一直在加新事件型別，不能因為沒見過就炸掉）；
- 但**結構壞掉的單一 event 會被丟掉並記 log**，而不是讓整包 webhook 回 4xx——
  因為 LINE 收到非 200 會重送整包，好的 event 就會被重播一次。這個「逐個驗證、
  壞的跳過」邏輯在 `WebhookBody._drop_malformed_events`（`events.py:128`）。

所有 pydantic 模型都 `extra="ignore"`，而且**只建模這個 router 真的會讀的欄位**——
LINE event 有幾十個欄位，我們只認 `type` / `webhookEventId` / `replyToken` /
`source` / `message`。

### 4c. 逐 event 分派（`adapter.py:_dispatch_event`）

每個 event 過三道快篩，任一不過就跳過（記 log），**不會**讓 webhook 失敗——
因為 envelope 層已經承諾回 200 了，LINE 的契約不允許事後對單一 event 報錯：

1. `event.type != "message"` → 跳過（follow、postback 等目前不處理）；
2. **Dedup**（`dedup.py`）：LINE 的投遞是 at-least-once，我們回 200 慢了它就重送。
   `EventDeduplicator` 用一個有界 dict（上限 1000，滿了淘汰最舊 10%）記住看過的
   `webhookEventId`。狀態是 in-process 的——單 worker 部署下夠用，這個限制明寫在
   docstring 裡（`dedup.py:14-16`）；
3. `room_key` 解析不出來 → 跳過。

### 4d. room_key：加上 channel 前綴（設計文件 §4.3 的核心）

`Event.room_key`（`events.py:106`）是 **LINE room_key 的唯一生產點**：

```
LINE 原生 id：  U9f3…e2   （只對 LINE API 有意義）
room_key：      line_U9f3…e2   （core 與所有下游用這個）
```

為什麼要前綴？因為 `room_key` 是**全系統的複合主鍵**，往下游一路變成：
容器名 `hermes_line_U9f3…`、資料目錄 `data/line_U9f3…/`、Google account_key、
hermes session id。不同通道的原生 id 沒有全域唯一保證，前綴讓它們永不相撞。

對應的**唯一反向轉換點**是 `LineAdapter._native_id`（`adapter.py:143`）：只有在
最後要呼叫 LINE 的 reply/push API 時，才把前綴剝掉還原成 LINE 認得的 id。
一個生產點、一個消費點，中間全程都是前綴鍵。

### 4e. 訊息 → 文字（`events.py` 的 `resolve_inbound_text`）

這裡是 CLAUDE.md Growth Discipline「4 分支改 dispatch table」規則的實例：

```python
_MESSAGE_HANDLERS = {
    "text":     _handle_text,        # 原文直接過
    "sticker":  _handle_sticker,     # → "[使用者傳送了貼圖：…]"
    "location": _handle_location,    # → "[使用者傳送了位置：…]"
    "image"/"audio"/"video"/"file": _download_and_note_media,
}
```

媒體的處理最值得看（`_download_and_note_media`，`events.py:179`）：
**router 不解讀媒體內容**。它用 LINE token 把二進位下載到
`data/<room_key>/incoming/<檔名>`——這個目錄正是該房間 Hermes 容器 bind-mount 的
資料夾——然後把訊息替換成一句話：

> `[使用者傳送了一個image檔案，已存放於 /opt/data/incoming/xxx.jpg，請視需要用你的工具讀取並回覆。]`

檔案落地即對容器內的 agent 可見，讓 agent 用自己的 vision/檔案工具去讀。
另外注意 `_resolve_media_filename` 有做 path traversal 防護（`Path(file_name).name`
剝掉目錄成分）。

### 4f. 排進 background task，立刻回 200

```python
background_tasks.add_task(self._process_and_reply, room_key, text, config, reply_token)
```

Hermes agent 一次呼叫可能跑幾十秒到幾分鐘，但 LINE 期待 webhook 快速回 200
（否則重送）。所以 handler 先回 200，真正的「問 agent → 送回覆」在 FastAPI
background task 裡跑。這也解釋了 dedup 為什麼重要——回 200 前的任何延遲都可能觸發重送。

---

## Step 5：channel-free 核心（`core.py`）

Background task 裡做的第一件事就是跨過抽象邊界：

```python
msg = InboundMessage(channel="line", room_key=room_key, text=text)
texts = await process_inbound(msg, config)   # ← 從這行起，世界裡沒有 LINE
```

`process_inbound`（`core.py:54`）只有三步：

1. **Google OAuth gate**：`check_google_authorization(room_key)` 回傳三態——
   `blocked`（只回授權訊息，不問 agent）／`notice`（提示 + 照常問 agent）／`ok`；
2. **容器解析**：`get_or_create_container(room_key)` 拿到（必要時建立）
   `hermes_<room_key>` 容器的 URL；
3. **問 agent**：`ask_hermes_agent(url, room_key, text)`。

兩個設計重點：

- **每一步各自 try/except、失敗記 log 回 None**——這個函式跑在 background task 裡，
  例外往上拋沒有人接得住，所以錯誤在這層就地吸收（`_ask_agent` 的 docstring 明講了
  這個契約）。
- **回傳值是 `list[str]`，不是「已送出」**。gate blocked 回 `[授權訊息]`；notice 回
  `[提示, agent回覆]`；正常回 `[agent回覆]`。誰去送、怎麼送，是呼叫端 adapter 的事——
  這就是 core 可以被 LINE 和 API 通道共用的原因。

---

## Step 6：把回覆送回 LINE（出站）

core 回傳 `list[str]` 後，控制權回到 adapter 側，走
`_deliver_texts` → `_deliver_reply` → `client.py` → `format.py`：

### 6a. Reply token 優先，Push 兜底（`adapter.py:199`）

LINE 有兩種送訊方式：**Reply**（免費，但 token 單次使用、約 60 秒過期）和
**Push**（計費）。策略是：

- 第一則文字嘗試用 reply token；後續文字一律 Push（token 只能答一次）；
- **不在本地猜 token 過沒過期**——agent 跑了兩分鐘 token 八成過期了，但我們直接送
  reply、讓 LINE 自己的拒絕（`ApiException`）觸發 Push fallback。LINE 的判斷比
  本地 TTL 猜測準確（`_deliver_reply` docstring 有寫這個取捨）。

### 6b. 出站格式化（`format.py`）

Agent 回的是 Markdown，但 LINE 文字泡泡**不支援 Markdown**，而且有硬限制：
單則 ≤5000 字、單次呼叫 ≤5 則。`format_for_line` 做兩件事：

1. `strip_markdown_preserving_urls`：拆 code fence、去粗斜體、`[label](url)` 改寫成
   `label (url)`（讓 LINE client 的自動連結還能點）、bullet 換成 `•`；
2. `split_for_line`：以 4500 字為軟上限切塊（優先在段落／行／空白斷開），最多 5 塊，
   還裝不下就在最後一塊以 `…` 截斷——寧可截斷也要保證單次呼叫送得完。

這一整個模組就是「LINE 送訊長度/則數限制」這個改動理由的家（CLAUDE.md 路由表第一列）。

---

## Step 7：對照組——API 通道（`api.py`）

看完 LINE 再看 API 通道，抽象的價值就顯出來了。這是給 TUI／mobile／dev 用的第一方通道，
**兩端都是我們自己的 client**，所以 LINE 的三大包袱它一個都沒有：

| | LINE adapter | API adapter |
|---|---|---|
| 驗證 | HMAC 簽章（第三方 wire format） | Bearer token（常數時間比較） |
| Dedup | 需要（LINE at-least-once 重送） | 不需要（HTTP 同步請求回應） |
| 回覆 | reply token / Push、非同步 background task | **同步**在 HTTP response 回 `{"replies": [...]}` |
| 出站格式化 | 去 Markdown + 切泡泡 | 無——原樣回 Markdown，渲染是各 client 的事 |
| 檔案數 | 6 個檔案 | 1 個檔案 142 行 |

設計文件 §4.4 的原則：**第一方通道不偽裝成 webhook**——不需要假裝有 wire format、
假裝非同步。它就是一個 `POST /webhooks/api/messages`，收 `{room_key, text}`，
呼叫同一個 `process_inbound`，把結果同步回給你。

唯一需要小心的是 `room_key` 驗證（`api.py:33`）：因為 room_key 會流進 docker 容器名
（hostname 有 63 字限制）和 Google account_key 的 regex，不能收任意字串，所以用
白名單 regex 只接受兩種形狀——`line_<LINE原生id>`（curl 進既有 LINE 房間 debug 用）
或 `api_<slug>`（此通道自己的房間）。

它也是最好的 debug 入口：

```bash
curl -s -X POST localhost:8000/webhooks/api/messages \
  -H "Authorization: Bearer $API_CHANNEL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room_key": "api_dev-mary", "text": "hello"}'
```

---

## Step 8：這個設計「刻意不做」的事

理解一個設計，一半在理解它拒絕了什麼（設計文件 §5）：

- **沒有 OutboundMessage 抽象**：core 回 `list[str]` 就好。等到真的有第二個通道
  需要結構化回覆（按鈕、卡片）再說，現在抽象是憑空猜需求。
- **Dedup 不上提到 core**：重送是 LINE 的通道特性，API 通道根本沒這問題。
  放進 core 就是逼所有通道扛 LINE 的包袱。
- **沒有動態 plugin 系統**：`enabled_adapters` 就是一個 hardcode 的 list。
  通道數量是個位數，動態發現是自找的複雜度。
- **`api.py` 不開資料夾**：一個檔案裝得下就不拆（§4.5）。`line/` 拆六檔是因為
  六種不同的改動理由，不是因為「通道都該長這樣」。

## Step 9：想加一個新通道（如 Telegram）要動哪裡？

1. 建 `channels/telegram/`，寫一個滿足 `ChannelAdapter` 的 adapter
   （name、`api_router()`；驗簽/解析/dedup 全自己包）；
2. 定自己的 room_key 前綴 `telegram_<native id>`，並在 `api.py` 的
   `_ROOM_KEY_RE` 加上新形狀（那裡的註解已預留這一步）；
3. 在 `enabled_adapters` 加一行（要 gate 就比照 API 通道用「不在清單」表達）；
4. core、`container_manager`、`hermes_client` **一行都不用改**——這就是驗收標準。

## 延伸閱讀

- [channel-interface-design.md](channel-interface-design.md) — 完整設計論證、prior art、§4.3 room_key 遷移細節
- [channel-interface-plan.md](channel-interface-plan.md) — 實作計畫與實作紀錄
- [line-hermes-message-flow.md](line-hermes-message-flow.md) — 訊息流的時序細節
- [architecture-c4.md](architecture-c4.md) — C4 架構圖
