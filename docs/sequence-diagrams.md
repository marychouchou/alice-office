# Sequence Diagrams 彙整 — Alice Office Router

依 2026-08-28 的程式碼現況繪製。這份文件的角色是**導覽索引＋關鍵路徑視覺化**，
比照 AGENTS.md「文件地圖」的精神：每張圖只畫到能看懂分支在哪裡分岔為止，實作
細節（欄位格式、錯誤處理總表、併發推理）一律連到對應的權威文件，不在此重複。
對應的 use case 總覽見 `docs/use-case-diagram.md`。

---

## 1. 1:1 一般訊息（happy path + reply/push fallback）

最基本的路徑：驗簽 → 立即回 200 → 背景任務才真正跑容器與 agent → reply token
優先、過期或被拒才 fallback push。

```mermaid
sequenceDiagram
    autonumber
    actor U as LINE 使用者
    participant LP as LINE Platform
    participant R as Router（LineAdapter）
    participant C as core.process_inbound
    participant H as Hermes 容器

    U->>LP: 傳送文字訊息
    LP->>R: POST /webhooks/line（x-line-signature）
    R->>R: verify_line_signature()
    R->>R: dedup 檢查 webhookEventId
    R->>R: resolve_inbound_text（text 直傳）
    R->>R: background_tasks.add_task(_process_and_reply)
    R-->>LP: 200 {"status":"ok"}
    Note over R,H: 以下在背景任務中執行
    R->>C: process_inbound(InboundMessage)
    C->>C: 取得該房間的 turn lock（同房間前一輪還在跑就排隊）
    C->>C: check_google_authorization → ok
    C->>C: get_or_create_container(room_key)
    C->>H: POST /v1/chat/completions（stream: true，SSE）<br/>X-Hermes-Session-Id: session_id_for(room_key, epoch)<br/>system=DIRECT_SYSTEM_PROMPT
    Note over C,H: 回覆以 SSE 串流接收：閒置逾時 HERMES_IDLE_TIMEOUT_SECONDS（Hermes 每 30 秒送 keepalive）、絕對上限 HERMES_REQUEST_TIMEOUT_SECONDS；詳見 router-hermes-agent-protocol.md
    H-->>C: 回覆文字 + usage.prompt_tokens
    C-->>R: [reply]
    R->>R: 去 Markdown + split_for_line
    alt 有 reply token 且未被拒
        R->>LP: Reply Message API
    else 無 token，或 reply 被拒（過期/已用）
        R->>LP: Push Message API（fallback）
    end
    LP->>U: 顯示回覆
```

細節（envelope 解析、多 event 批次、媒體同步下載、錯誤處理總表）見
`docs/line-hermes-message-flow.md`；router↔container 協定欄位見
`docs/router-hermes-agent-protocol.md`。

> 「取得該房間的 turn lock」沒有單獨畫一條 lane：它是 `core._route` 裡一個 process-local
> 的 per-room `asyncio.Lock`，同房間的第二則訊息在這裡排隊（FIFO，不丟訊息，等到時記一筆
> `room_turn_queued` log），不同房間互不影響。`system=DIRECT_SYSTEM_PROMPT` 是部署層對
> 1:1 回覆形狀的規範（精簡、大任務分段交付），見 `docs/prd.md` FR-01。

---

## 2. 群組訊息 — 未點名（僅寫入背景，不回覆）

`userA 跟 userB 問早` 這種情境：訊息被記錄成背景脈絡，但完全不呼叫 agent、
不送任何 LINE 訊息。

```mermaid
sequenceDiagram
    autonumber
    actor A as 群組成員 A
    participant LP as LINE Platform
    participant R as Router（LineAdapter）
    participant C as core.process_inbound
    participant GC as group_context

    A->>LP: 「早安」（無 @mention、無呼叫詞）
    LP->>R: POST /webhooks/line
    R->>R: verify + dedup
    R->>R: resolve_inbound_text（text 直傳）
    R->>R: resolve_sender_name（LINE member profile API，TTL cache）
    R->>R: _is_addressed(event, text) → False
    R->>R: background_tasks.add_task(..., is_group=True, addressed=False)
    R-->>LP: 200 {"status":"ok"}
    Note over R,GC: 背景任務
    R->>C: process_inbound(InboundMessage)
    C->>C: is_group ∧ ¬addressed → observe 短路（先於 OAuth gate）
    C->>GC: record_observed(sender_id, sender_name, text)
    GC->>GC: append，超過 GROUP_OBSERVED_MAX_MESSAGES 就丟最舊
    C-->>R: []
    Note over R: 不呼叫 agent、不送任何 LINE 訊息
```

觸發判斷（`@mention` vs 呼叫詞）與 observed buffer 的完整格式見
`docs/group-chat-design.md` §4、§6。

---

## 3. 群組訊息 — 點名（讀 buffer → 組 prompt → 呼叫 agent → 清 buffer / silence 判斷）

點名後才把先前的背景一次帶給 agent；agent 回覆若是 silence token 則整段被丟棄，
buffer 只在 agent 成功回覆後才清空（失敗保留，避免背景遺失）。

```mermaid
sequenceDiagram
    autonumber
    actor U as 點名的群組成員
    participant LP as LINE Platform
    participant R as Router（LineAdapter）
    participant C as core
    participant GC as group_context
    participant H as Hermes 容器

    U->>LP: 「@Alice 幫我排下週的會議」
    LP->>R: POST /webhooks/line
    R->>R: verify + dedup + 剝除自我 @mention
    R->>R: resolve_sender_name
    R->>R: _is_addressed → True（mention_is_self）
    R->>R: background_tasks.add_task(..., addressed=True)
    R-->>LP: 200 {"status":"ok"}
    Note over R,H: 背景任務
    R->>C: process_inbound(InboundMessage)
    C->>GC: peek_observed(room_key)
    GC-->>C: 先前累積的背景訊息（可能為空）
    C->>C: build_group_prompt(observed, msg)<br/>[name|id] 標籤 + 背景區塊
    C->>H: POST /v1/chat/completions（stream: true，SSE）<br/>system=GROUP_SYSTEM_PROMPT
    H-->>C: 回覆文字
    C->>GC: clear_observed(peeked)（依 timestamp cutoff，非位置；<br/>agent 成功回覆就清，不論是否 silence）
    alt 回覆是 silence token（NO_REPLY 等）
        C->>C: is_silence → True
        Note over C: 回傳 None，不送任何訊息
    else 正常回覆
        C-->>R: [reply]
        R->>LP: reply / push
        LP->>U: 顯示回覆
    end
```

Prompt 組裝格式（`[名稱|ID]` 標籤、injection 防護）、silence token 集合見
`docs/group-chat-design.md` §7；`group_context.py` 模組 docstring 有 buffer 的
併發推理（單 worker、peek→clear 之間的空窗如何處理）。

---

## 4. Bot 被拉進群組（join event → 自我介紹）

`join` event 直接在 adapter 層用 reply token 回固定文案，完全不經 `core`、
不問 agent。

```mermaid
sequenceDiagram
    autonumber
    actor M as 群組成員（邀請 bot）
    participant LP as LINE Platform
    participant R as Router（LineAdapter）

    M->>LP: 把 bot 加入群組
    LP->>R: POST /webhooks/line（join event，帶 replyToken）
    R->>R: dedup 檢查 webhookEventId
    R->>R: _schedule_join_greeting
    R-->>LP: 200 {"status":"ok"}
    Note over R: 背景任務（不經 core，不呼叫 agent）
    R->>LP: Reply Message API（固定繁中自我介紹＋使用說明）
    LP->>M: 群組收到自我介紹
```

文案常數與 `leave`/`memberLeft`/`memberJoined` 目前刻意不處理的原因見
`docs/group-chat-design.md` §9。

---

## 5. Google OAuth gate 三態（ok／notice／blocked）對訊息流程的影響

`check_google_authorization` 每則要進 agent 的訊息都跑一次。**重點：`blocked`
時完全不呼叫 agent**——這一步在 observe 短路與手動 reset 之後、`_reply_for`
之前執行。三態判斷本身的邏輯圖見 README「訊息授權判斷流程」，這裡補一張真正的
時序版本。

```mermaid
sequenceDiagram
    autonumber
    actor U as 使用者
    participant R as Router（core）
    participant OA as google_oauth
    participant H as Hermes 容器

    U->>R: 訊息（已通過 observe / reset 短路）
    R->>OA: check_google_authorization(room_key)
    alt tokens.json 不存在，或過期且無 refresh_token
        OA-->>R: ("blocked", 授權連結文案)
        Note over R,H: 完全不呼叫 agent
        R-->>U: 只回授權連結
    else 有 token 但缺 Drive scope
        OA-->>R: ("notice", 重新授權提示)
        R->>H: 照常呼叫 agent（calendar/gmail 可用）
        H-->>R: 回覆
        R-->>U: [提示, agent回覆]
    else 授權齊全
        OA-->>R: ("ok", None)
        R->>H: 照常呼叫 agent
        H-->>R: 回覆
        R-->>U: [agent回覆]
    end
```

token 過期/scope 判斷細節、`account_key` 小寫轉換規則、影響既有房間的注意事項見
README「Google Workspace 整合」與 `docs/google-workspace-integration-summary.md`。

---

## 6. Session 重置／自動輪替 — 精簡總覽

換新 session 有**兩條彼此獨立的路徑**，不要混為一談：

- **手動重置**（`/new`／`/reset`／`新對話`；群組要先點名，如 `@bot /new` 或「呼叫詞 +
  指令」）：`process_inbound` 在 OAuth gate **之前**以 `check_reset_command` 短路，
  `reset_session` 把 epoch +1、清 token 水位，群組另清 observed buffer，直接回固定
  確認文案——**不經 `begin_turn`、不要交接摘要、完全不呼叫 agent**（乾淨重來）。
- **自動輪替**（距上次進 agent 的訊息超過 `SESSION_IDLE_RESET_MINUTES`（預設 1 天）、
  上一輪 token 用量超標）：一則正常要進 agent 的
  訊息在 `_ask_agent` 裡由 `begin_turn` 同一次同步呼叫判斷並原子地 bump epoch，
  命中就先跟**剛退役**的 session 要一份 ≤300 字交接摘要、以 user message 前置注入
  新 epoch 的第一則訊息。

**這裡不重畫細節**，權威版本在 `docs/session-hygiene.md`：狀態檔欄位與 session id 推導見
「機制：router 自管 session epoch」、觸發條件與手動重置的時序圖見「三種輪替」、epoch CAS
併發推理見「併發推理（single-worker 前提）」、交接失敗的取捨見「交接流程與注入格式」與
「已知限制」。自動輪替＋交接的時序圖只畫在本節。

```mermaid
sequenceDiagram
    autonumber
    actor U as 使用者
    participant R as Router（core）
    participant S as session.json
    participant H as Hermes 容器

    alt 手動重置指令（/new、/reset、新對話）
        U->>R: 「/new」（群組需先點名）
        R->>R: check_reset_command 命中（先於 OAuth gate）
        R->>S: reset_session：epoch N → N+1、清 token 水位
        Note over R: 群組另清 observed buffer<br/>不經 begin_turn、不要交接摘要
        R-->>U: 固定確認文案（完全不呼叫 Hermes）
    else 一般訊息（已通過 observe／reset 短路與 OAuth gate、容器已就緒）
        U->>R: 訊息
        R->>S: begin_turn：評估門檻（未命中則只蓋活動時間）
        opt 任一門檻命中且狀態寫入成功 → 原子 bump epoch N → N+1
            R->>H: 對剛退役 session（epoch N 的 id）索取 ≤300 字交接摘要
            H-->>R: 摘要（失敗則放棄，乾淨開新 epoch）
        end
        R->>H: POST /v1/chat/completions（stream: true）<br/>X-Hermes-Session-Id: session_id_for(room_key, epoch)<br/>有摘要時前置注入 user message
        H-->>R: 回覆 + usage.prompt_tokens
        R->>S: complete_turn：記 token 水位（epoch CAS）
        R-->>U: 回覆（使用者對輪替本身無感）
        Note over R,H: 以上為成功路徑。agent 失敗 → 不記水位、不回覆<br/>群組回覆是 silence token → 不送出
    end
```

---

## 7. Container 冷啟動（第一次訊息進某房間）

第一則訊息落進一個還沒有 container 的房間時，seed（`SOUL.md`／`config.yaml`／
`mcp`／`plugins`）在 `docker run` **之前**完成——因為 `config.yaml` 的渲染要讀
剛 seed 出來的 MCP manifest，且 `SOUL.md` 一旦晚於 `docker run`，Hermes 會自己
先生一份預設版、之後就永遠蓋不掉（write-once）。

```mermaid
sequenceDiagram
    autonumber
    participant R as Router（core → container_manager）
    participant D as Docker Engine
    participant H as Hermes 容器

    R->>D: containers.get("hermes_" + room_key)
    D-->>R: NotFound
    R->>R: _ensure_data_dir / room_seed.ensure_soul_seed /<br/>room_seed.ensure_mcp_seed / room_seed.ensure_plugin_seed /<br/>_ensure_config_yaml / room_seed.ensure_google_seed（write-once seed；<br/>google 部分視部署是否啟用 Google OAuth，未啟用則 no-op）
    R->>D: docker run（image、volume /opt/data、network、<br/>env: API_SERVER_KEY 等，command: gateway run）
    D->>H: 建立並啟動容器
    loop 每秒一次，最多 60 秒
        R->>H: GET /health
    end
    H-->>R: 200 OK（ready）
    R->>H: POST /v1/chat/completions（第一個真正的 chat completion，stream: true）
    H-->>R: 回覆
```

命名規則、URL 解析（container 化 vs host 模式）、write-once seed 的完整規則見
`docs/router-hermes-agent-protocol.md`「前置步驟」節與 AGENTS.md
「Hermes Container Model」。

---

## 8. API channel 請求（同步回覆，不經 LINE 驗簽/reply token）

第一方通道給 TUI／mobile／dev 用：Bearer 驗證取代 HMAC 簽章、同一個
`process_inbound` 管線，但**同步**在 HTTP response 裡回原始 markdown（不去
Markdown、不分段、不經 background task）。

```mermaid
sequenceDiagram
    autonumber
    actor Dev as 開發者／測試者
    participant R as Router（ApiChannelAdapter）
    participant C as core.process_inbound
    participant H as Hermes 容器

    Dev->>R: POST /webhooks/api/messages<br/>Authorization: Bearer API_CHANNEL_TOKEN<br/>{room_key, text}
    R->>R: pydantic 驗證 request body（room_key 需 line_* 或 api_*、text 非空，<br/>否則 422；FastAPI 在進入 handler 前就驗證，早於下一步的 bearer 驗證）
    R->>R: _verify_bearer（常數時間比對）
    R->>C: await process_inbound(InboundMessage)
    C->>H: gate → 容器 → agent（與 1:1 路徑完全相同）
    H-->>C: 回覆
    C-->>R: [replies]
    R-->>Dev: 200 {"replies": [...]}（原始 markdown，同步回應）
```

與 LINE adapter 的逐項對照（驗證、dedup、回覆時機、出站格式化）見
`docs/channels-walkthrough.md` Step 7。

---

## 9. Webhook 事件去重（同一個 webhookEventId 重送）

LINE 的 webhook 投遞是 at-least-once；router 用 in-memory、有界的
`EventDeduplicator` 擋掉重送，讓使用者不會收到兩次回覆。

```mermaid
sequenceDiagram
    autonumber
    actor U as LINE 使用者
    participant LP as LINE Platform
    participant R as Router（LineAdapter）
    participant Ded as EventDeduplicator

    U->>LP: 傳送訊息
    LP->>R: POST /webhooks/line（webhookEventId=E1）
    R->>Ded: is_duplicate(E1)
    Ded-->>R: False（記錄 E1）
    R->>R: 正常處理（排入背景任務）
    R-->>LP: 200 {"status":"ok"}
    Note over LP,R: router 回應太慢，或 LINE 判定逾時
    LP->>R: 重送同一 event（webhookEventId=E1）
    R->>Ded: is_duplicate(E1)
    Ded-->>R: True（命中）
    R->>R: 記 log 後跳過，不建立背景任務
    R-->>LP: 200 {"status":"ok"}
    Note over R: 使用者不會收到第二次回覆；<br/>in-memory、per-process、不跨 worker
```

有界策略（上限 1000，滿了淘汰最舊 10%）與跨 worker 的限制見
`docs/line-hermes-message-flow.md`「關鍵設計要點小結」。

---

## 與其他文件的關係

- use case 總覽（誰、能做什麼）：`docs/use-case-diagram.md`
- 系統三層結構：`docs/architecture-c4.md`
- 群組聊天完整設計：`docs/group-chat-design.md`
- Session 輪替權威版本：`docs/session-hygiene.md`
- 訊息流程逐檔導讀：`docs/channels-walkthrough.md`、`docs/line-hermes-message-flow.md`
- router↔container 協定：`docs/router-hermes-agent-protocol.md`
