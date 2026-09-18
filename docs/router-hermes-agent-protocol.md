# Router ↔ Hermes Agent Container 通訊協定

聚焦說明：`alice-office-router` 怎麼把訊息送進某個聊天室專屬的 Hermes Agent
container，以及 container 內的 Hermes Agent 怎麼收、怎麼回。內容依現行實作整理
（`core.py`、`container_manager.py`、`hermes_client.py`），非設計文件。

範圍**不含** LINE Platform 端的簽章驗證、webhook 事件解析與 Push 回覆——那部分見
`docs/line-hermes-message-flow.md`；為何整體架構選擇這種 router-issued
`api_server` 呼叫而非 Hermes 內建 LINE adapter，見
`docs/hermes-agent-line-gateway-comparison.md`。

## 一句話總結

Router 與 Hermes container 之間走同一個 HTTP 介面：Hermes 內建的
OpenAI-compatible `api_server` platform。主線是 `POST /v1/chat/completions`——
純文字進、純文字出；container 完全不知道 LINE 的存在，也拿不到任何 LINE
憑證。同一個介面上還有一條輔助線 `GET /api/sessions/{session_id}`
（2026-09-15 加入，見下方「輔助請求」），只用來讀這一輪打了幾次工具／幾次 LLM
API 供 log 使用，不影響對話本身。

## 前置步驟：Router 怎麼找到 container 的位址

在能發 request 之前，router 得先知道要打去哪個 URL。這一步由
`get_or_create_container(room_id, config)` 負責（`container_manager.py:226`），
用模組層級的 `threading.Lock` 避免同房間併發請求造成重複建立：

```mermaid
flowchart TD
    A["get_or_create_container(room_id)"] --> B["docker.client.containers.get('hermes_' + room_id)"]
    B -- NotFound --> C["_create_container()\ndocker run <HERMES_IMAGE>\ncommand: gateway run\nvolume: HOST_DATA_DIR/room_id → /opt/data\nnetwork: HERMES_NETWORK\nenv: API_SERVER_KEY, API_SERVER_HOST=0.0.0.0, LLM_API_KEY"]
    C --> G[_get_container_url]
    B -- 找到但 status != running --> E[container.start]
    E --> G
    B -- 找到且 running --> G
    G --> I["輪詢 GET url/health\n每秒一次，最多 60 秒\n（已就緒的第一次就回）"]
    I -- 200 --> J[回傳 URL]
    I -- 逾時 --> K[raise RuntimeError]
```

要點：

- **命名規則**：container 名稱固定為 `hermes_{room_id}`，一個聊天室對應一個
  container，用 Docker 做硬隔離（`container_manager.py:248`）。
- **URL 解析**（`_get_container_url()`，`container_manager.py:148-178`）：
  - `ROUTER_IN_DOCKER=True`（正式部署）時，router 跟 Hermes container 在同一個
    Docker network 上，直接用 container name 當 hostname：
    `http://hermes_{room_id}:{HERMES_INTERNAL_PORT}`。
  - `ROUTER_IN_DOCKER=False`（本機 macOS 開發）時，router 跑在 host 上，改讀
    container 啟動時動態發布的 host port：`http://localhost:{host_port}`。
- **環境變數**（`_build_container_env()`，`container_manager.py:54-74`）：只給
  `API_SERVER_KEY`、`API_SERVER_HOST=0.0.0.0`、（若有設定）`LLM_API_KEY`。**不傳
  任何 `LINE_*` 憑證**——container 無法自行對 LINE API 做任何呼叫。
- **啟動指令是 `gateway run`**：只啟用 Hermes 內建的 `api_server` platform，不
  啟用內建的 LINE adapter（Hermes 原生支援 20 種 platform adapter，這裡刻意只開
  一種）。
- 三條路徑都會輪詢 `/health` 再回傳 URL（真實 Hermes image 要跑完 s6
  supervision、skill sync、gateway startup，比先前的 mock 慢很多）。2026-09-15 起
  不再對「已 running」跳過等待：背景暖機（`core.warm_room`）和
  operator `docker restart` 都會讓 container 先 running、api_server 晚一步才起來。
  已就緒的 container 第一次 poll 就回，穩態成本是每則一次 GET。

## 核心請求：`ask_hermes_agent()`

`hermes_client.py:12`，拿到 base URL 後直接呼叫：

```
POST {base_url}/v1/chat/completions
Headers:
  Authorization: Bearer {HERMES_API_SERVER_KEY}
  X-Hermes-Session-Id: {session_id}
Body:
  {"messages": [{"role": "user", "content": "<使用者訊息文字>"}],
   "stream": true}
```

- **`X-Hermes-Session-Id: session_id`**：讓同一聊天室的對話在 Hermes 端維持 session
  連續性；不同房間天生是不同 container，互不相通。session id **不再固定等於
  `room_key`**：router 依房間目前的 session epoch 推導（`session_hygiene.session_id_for`）
  ——epoch 0 送裸 `room_key`（與既有 session 相容），epoch N>0 送 `room_key#N`。換一個
  新值 = Hermes 靜默開全新 session，是本 repo 控制 context 成長的手段，完整規則見
  `docs/session-hygiene.md`。
- **`Authorization: Bearer`**：跟容器建立時注入的 `API_SERVER_KEY` 比對，是
  router↔container 唯一的驗證機制。
- **`"stream": true`（SSE）**：router 一律用 streaming 模式呼叫。**不是**為了逐字顯示
  ——回覆仍然是整包送回 LINE——而是為了拿到「agent 還活著」的訊號：Hermes 的
  `api_server` 在 streaming 期間每靜默 30 秒就寫一行 `: keepalive` 註解
  （`CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0`），**工具執行中也照送**，所以
  「還活著」＝「還有 bytes 進來」。
- **SSE 框格**：`Content-Type: text/event-stream`，事件是 `data: <json>\n\n`；以 `:`
  開頭的是註解（keepalive）必須忽略；串流以 `data: [DONE]` 結束。
  - 內容 chunk：`{"object":"chat.completion.chunk","choices":[{"index":0,
    "delta":{"content":"..."},"finish_reason":null}]}`；第一個 chunk 的 delta 可能只有
    `{"role":"assistant"}`、沒有 content。
  - 結束 chunk：帶 `choices[0].finish_reason`（`stop`／截斷時 `length`／其他）與
    `usage`；`finish_reason != "stop"` 時另外可能帶 `error: {message, type}` 與 Hermes
    自己的 `hermes: {completed, partial, failed, error, error_code}` 區塊。
  - 解析壞掉的 `data:` 行只跳過並記 warning（`hermes_agent_chunk_skipped`），不中止整輪
    ——一格壞掉的 frame 不值得賠掉整輪回覆。
  - **多輪工具呼叫時，`finish_reason` 全程是 `null` 直到真正結束**：一輪牽涉多次工具
    呼叫的對話，Hermes 把每一輪產生的文字都當普通 content chunk 吐出來（含每次呼叫
    工具前 model 自己講的旁白，例如「這個網址不行，換一個」），協定本身**不提供任何
    「這段文字屬於哪一輪」的欄位**（`gateway/platforms/api_server.py` 的
    `_write_real_streaming_sse`：非 tool-progress 的每個 item 都原樣包成
    `chat.completion.chunk`，`finish_reason` 寫死 `null`）。唯一的分界是 Hermes 在
    真正執行工具前送出的具名事件 `event: hermes.tool.progress\ndata: {...}`
    （tool-start 用，不是 `data:` 開頭的預設事件）。`_consume_stream` 靠它作分界：
    每收到一次這個具名事件就把目前累積的文字清空重新收集，所以 `AgentReply.text`
    只會是**最後一輪工具呼叫之後**產生的內容——之前幾輪的旁白仍然進了 Hermes 自己的
    `state.db`（agent 判斷要不要繼續工具呼叫用得到完整歷史），只是不會被轉送到 LINE。
    沒有工具呼叫的單輪對話不受影響（沒有這個具名事件，行為跟以前一樣是全部串接）。
- **兩條逾時，意義不同**（任一條踩到，使用者都收到固定提示，不再靜默）：
  - **Idle（靜默上限）**：`HERMES_IDLE_TIMEOUT_SECONDS`，預設 120 秒。連 keepalive 都
    沒了 ⇒ 容器／agent 卡死。實作是 httpx 的 read timeout，逾時拋 `httpx.ReadTimeout`。
  - **Ceiling（絕對上限）**：`HERMES_REQUEST_TIMEOUT_SECONDS`，預設 3600 秒。keepalive
    一直來但永遠不結束時的保險絲，正常不會踩到。實作是 `asyncio.timeout()`，逾時拋
    `TimeoutError`。

  判定「活著」靠 idle 而不是總時長：一輪純推理（~85 tok/s）跑十幾分鐘是正常的，用總時
  長設限只會兩頭不討好——設小了把好答案丟掉（那輪的回覆孤零零留在 Hermes session
  裡），設大了又等於死掉的 agent 沒人發現。兩條都比 LINE webhook 本身的等待時間長得多
  ——這也是為什麼整段呼叫必須在 `BackgroundTasks` 裡進行，而不是同步等待再回應 LINE。
- **回應解析**：`ask_hermes_agent` 回傳 `AgentReply{text, prompt_tokens}`。
  - `text` = 最後一輪工具呼叫之後的 `delta.content` 依序串接（見上方「多輪工具呼叫」
    一點）；沒有工具呼叫的單輪對話就是全部 `delta.content` 依序串接。
  - **成功／截斷／失敗**：`hermes.failed` 為真（或 `finish_reason` 不是 `stop`／`length`
    且帶了錯誤訊息）→ `raise ValueError("Hermes agent failed: ...")`；
    `finish_reason == "length"`（＝`hermes.partial`）→ 回傳截到一半的文字，另記 warning
    `hermes_agent_truncated`（半個答案好過沒答案）；串流結束但一個字都沒有 →
    `raise ValueError("Hermes agent response had no content")`。
  - `prompt_tokens` 取自結束 chunk 的 `usage.prompt_tokens`（缺 `usage` 或回報 <=0 →
    正規化為 `None`，0 是 server「沒統計」的預設），供 session 輪替的 token 水位判斷用
    （累計語意警語見 `docs/session-hygiene.md`）。
  - `tool_calls`／`api_calls`（2026-09-15 加入）：這一輪 Hermes 內部的工具呼叫次數／LLM
    API 呼叫次數，供 `docs/logging-design.md` 的 `hermes_agent_call` log 事件與
    turn envelope 使用（動機：診斷「一輪考卷從 3 分鐘拖到 15 分鐘」這類重試風暴，不用
    SSH 進容器翻 `logs/agent.log`）。`/v1/chat/completions` 的 `usage` 只有 OpenAI 的
    token 三件組，不帶這兩個數字——見下方「輔助請求」。

### 輔助請求：`GET /api/sessions/{session_id}`（供 `tool_calls`／`api_calls` 取差值）

`ask_hermes_agent` 在自己的 POST 之前、之後各打一次這個端點（同一個 Bearer token，
`hermes_client._fetch_session_counts`），把回應的 `session.tool_call_count`／
`session.api_call_count` 前後相減，當作這一輪的貢獻（`hermes_client._count_delta`）：

- 這兩個數字是**整個 session 的累計值**，不是單輪——`GET /api/sessions/{id}` 是 Hermes
  唯一公開它們的地方，所以只能用前後取差值的方式還原單輪貢獻。
- 呼叫前的 session 若還不存在（新 epoch 的第一輪，Hermes 要等第一次
  `/v1/chat/completions` 才會建立該 session）→ 404，視為基準 0（不是「未知」）。
- 任一次讀取失敗（逾時、非 2xx、回應格式不符）→ 兩個欄位一律 `None`，代表「不知道」而
  非「這一輪打了 0 次」；這條輔助請求的逾時獨立設定（10 秒），從不借用這一輪自己的
  idle／ceiling 預算，讀取失敗也從不讓這一輪的主要回覆跟著失敗（fail-soft）。
- `session_id` 可能帶 `#`（輪替後的 epoch 後綴），組 URL 時必須 percent-encode
  （`urllib.parse.quote(session_id, safe="")`），否則會被當成 URL fragment 整段砍掉。

**已知取捨（未做）**：Hermes 內部工具失敗時只在容器內
`logs/agent.log`／`errors.log` 印一行
`WARNING agent.tool_executor: Tool X returned error`（`agent/tool_executor.py` 的
`_detect_tool_failure`），`state.db` 與 `/api/sessions` 都沒有對應的「這次呼叫是否失敗」
欄位；chat completions 的 SSE `hermes.tool.progress` 事件也不帶錯誤旗標（那個欄位只接進
`/v1/runs` 的事件回呼，我們用的 `/v1/chat/completions` 沒有接這條線）。要拿到
「這一輪工具呼叫失敗幾次」目前只剩 tail 這行 log 一條路，見 `docs/logging-design.md`
§5.1 的同一則補充。

### 暖機探針與 `DELETE /api/sessions/{session_id}`

Router 可以在背景把房間暖起來（`core.warm_room` → `core._run_warmup`），**兩步**：
（2026-09-18 起觸發點不再是「Google gate 擋下訊息」——那個狀態已刪除；改接 LINE
`follow`／`join`，見 `docs/google-auth-per-member-plan.md` §3.5，目前尚未接上。）

1. `get_or_create_container`：把容器叫起來（30–60 秒的冷啟動）。
2. **暖機探針**：對這個容器送一輪丟棄用的對話，session id 固定
   `warmup-probe`（`core.WARMUP_SESSION_ID`），內容只是一句「回 OK 就好」
   （`core.WARMUP_PROMPT`），ceiling 用自己的 120 秒（`_WARMUP_MAX_SECONDS`），
   不借用給真實對話用的 `HERMES_REQUEST_TIMEOUT_SECONDS`。

第 2 步存在的理由：**容器 running 不等於 agent 熱**。Hermes 進程在「這個進程的第一輪
對話」會做一次 tool registry 探測（browser／terminal／image-gen／web-key 能力檢查，
外加兩次 vision auto-detect 會去打 models.dev 然後慢慢失敗），實測約 4.5 秒；結果
memoize 在進程層（`model_tools._tool_defs_cache`，key 是 toolsets ＋ registry
generation ＋ `config.yaml` mtime），所以只要有「某一輪」先付掉，同一個容器進程之後
每一輪都是 11 ms 的 init。把這一輪換成 router 自己的探針，使用者授權後的第一句話就
不用等。

探針跑完（成功或失敗都一樣）立刻刪掉那個 session：

```
DELETE {base_url}/api/sessions/{session_id}
Headers:
  Authorization: Bearer {HERMES_API_SERVER_KEY}
→ 200 {"object":"hermes.session.deleted","id":"warmup-probe","deleted":true}
```

- **為什麼一定要刪**：Hermes 的 `session_search` 工具可以跨 session 搜同一個房間，
  留著的探針會變成使用者搜得到的「對話紀錄」。刪除會同時移除 `state.db` 裡的 session
  row 與它的訊息。
- 這是 router 唯一一個會刪 Hermes 端資料的呼叫（`hermes_client.delete_hermes_session`）。
- **404 視為成功**：探針若在 Hermes 真正建立 session 之前就失敗，本來就沒東西可刪，
  後置條件已經成立。其他非 2xx → `raise_for_status()`，由 `core` 記
  `Could not delete warm-up session for room ...`（WARNING）。
- `session_id` 一樣要 percent-encode（同上一節的理由）。
- 探針失敗（`httpx.HTTPError`／`ValueError`／`TimeoutError`）只記一行 WARNING
  `Agent warm-up probe failed for room ...`，不影響那則已經送出的授權連結；房間不會被
  標記成已探測，下一則被擋的訊息會再試一次。
- **一個 router 進程對一個房間只探一次**（`core._probed`）：探針是一次真的 LLM 呼叫
  （約 28k prompt tokens），已經熱的容器不值得再付一次。已知取捨：operator 手動
  `docker restart` 某房間的容器而 router 沒重啟 → 該房間下一輪真實對話自己付一次
  4.5 秒；router 重啟 → 每個房間最多多探一次。

### 媒體訊息不走這條 API body

圖片/語音/影片/檔案**不會**編碼進 `/v1/chat/completions` 的 request body（Hermes
`api_server` 本身也只吃 `image_url`/inline base64 圖片，不吃 file/audio/video
part）。Router 改用「共享檔案落地」策略（`channels/line/events.py::_download_and_note_media()`）：

1. Router 用 LINE Content API 把二進位下載下來，寫進
   `config.DATA_DIR/{room_id}/incoming/{filename}`。
2. 該路徑透過 Docker volume mount 對應到 container 內的
   `{CONTAINER_DATA_DIR}/incoming/{filename}`（即 `/opt/data/incoming/...`）。
3. Router 只送一則文字通知作為 `/v1/chat/completions` 的 `content`，例如：
   `[使用者傳送了一個 image 檔案，已存放於 /opt/data/incoming/xxx.jpg，請視需要
   用你的工具讀取並回覆。]`
4. Container 內真正的 Hermes agent 用自己的 vision/STT/檔案工具讀那個路徑，
   router 不試圖解讀媒體內容本身。

## Hermes Agent 端怎麼收

Container 內由 `api_server` platform（`gateway run` 啟動的唯一介面）接手：

1. 比對 `Authorization: Bearer` 是否等於自己的 `API_SERVER_KEY`。
2. 依 `X-Hermes-Session-Id` 找回（或新建）對應的 session，維持對話上下文。
3. 把 `messages[-1].content` 當使用者輸入交給同進程內的 agent 核心（skills、
   記憶、LLM 呼叫都在 container 內部完成，對 router 而言是黑盒）。
4. 因為請求帶了 `"stream": true`，回覆以 SSE 逐塊送出（`chat.completion.chunk`），
   期間每靜默 30 秒補一行 `: keepalive`，最後一個 chunk 帶 `finish_reason`／`usage`／
   `hermes` 區塊，再以 `data: [DONE]` 收尾；router 端把所有 `delta.content` 串回一段
   完整文字。

Hermes agent 完全不知道自己在跟 LINE 互動——它看到的只是「api_server 收到一則
帶 session id 的文字訊息」，跟 LINE 的耦合、驗簽、Push/Reply 全部由 router 在這
一層之外處理完畢。

## 時序圖

```mermaid
sequenceDiagram
    participant R as alice-office-router
    participant D as Docker Engine
    participant H as Hermes Agent container<br/>(hermes_#lt;room_id#gt;, api_server platform)

    Note over R: _process_and_reply(room_id, text)
    R->>D: get_or_create_container(room_id)
    alt container 不存在
        D->>H: docker run（gateway run，env: API_SERVER_KEY 等，volume: /opt/data）
    else container 已停止
        D->>H: container.start()
    end
    loop 每秒一次，最多 60 秒（已就緒的第一次就回）
        R->>H: GET /health
    end
    H-->>R: 200 OK（ready）
    D-->>R: container URL（Docker DNS 或 localhost:port）

    Note over R,H: 核心請求（SSE streaming）
    R->>H: POST /v1/chat/completions<br/>Authorization: Bearer HERMES_API_SERVER_KEY<br/>X-Hermes-Session-Id: session_id（room_key 或 room_key#epoch）<br/>body: {"messages":[...], "stream": true}
    H->>H: 驗證 Bearer token
    H->>H: 依 X-Hermes-Session-Id 解析/建立 session
    H-->>R: 200 OK, Content-Type: text/event-stream
    loop agent 核心處理（skills / 記憶 / LLM 呼叫，黑盒）
        H-->>R: ": keepalive"（每靜默 30 秒，工具執行中也送）
        H-->>R: data: {...,"delta":{"content":"..."}}
    end
    H-->>R: data: {...,"finish_reason":"stop","usage":{...},"hermes":{...}}
    H-->>R: data: [DONE]
    Note over R: 串接所有 delta.content；<br/>靜默超過 idle timeout → ReadTimeout，<br/>整輪超過 ceiling → TimeoutError

    Note over R: reply_text 交給 _deliver_reply() 送回 LINE（見 line-hermes-message-flow.md）
```

## 錯誤處理（這一段涉及的部分）

| 階段 | 失敗條件 | 行為 |
|---|---|---|
| `get_or_create_container` | Docker API 錯誤 | log error，`_process_and_reply` 中止，使用者收不到回覆 |
| `get_or_create_container` | `/health` 60 秒內未回 200 | `raise RuntimeError`，同上中止 |
| `ask_hermes_agent` | HTTP 錯誤（非 2xx、連線失敗） | `httpx.HTTPError`，log error，回固定提示 |
| `ask_hermes_agent` | 串流靜默超過 `HERMES_IDLE_TIMEOUT_SECONDS` | `httpx.ReadTimeout`，log error（idle），回逾時提示 |
| `ask_hermes_agent` | 整輪超過 `HERMES_REQUEST_TIMEOUT_SECONDS` | `TimeoutError`，log error（ceiling），回逾時提示 |
| `ask_hermes_agent` | `hermes.failed` 或串流無任何內容 | `raise ValueError`，log error，回固定提示 |
| `ask_hermes_agent` | `finish_reason == "length"`（截斷） | **不算失敗**：回傳截到一半的文字，log warning `hermes_agent_truncated` |

`core.py::_ask_agent()` 對這兩步各自 `try/except`，失敗只記 log 不
往外拋——此時 LINE webhook 早已回過 200，沒有 HTTP response 可以再回錯誤給任何
人，只能從 router 的 log 觀察到。

## 關鍵設計要點

- **單一介面，三條路**：router↔container 只走 Hermes 的 `api_server` platform，
  沒有其他 API 或直接的 IPC。對話走 `/v1/chat/completions`；`GET
  /api/sessions/{session_id}` 是唯讀的輔助線，只供 log 用的呼叫計數，從不影響
  對話或失敗時的使用者回覆（fail-soft，見「輔助請求」）；`DELETE
  /api/sessions/{session_id}` 只在暖機探針善後時用，是 router 唯一會刪 Hermes 端
  資料的呼叫。
- **隔離靠 container，不靠協定**：協定本身（Bearer + session header）很單純，
  真正的房間隔離來自「一個 room_id 一個 Docker container」這個更外層的設計。
- **container 對 LINE 零知情**：不傳憑證、不傳 LINE 專屬欄位，agent 收到的只是
  「文字 + session id」，回覆分段、Markdown 清理等 LINE 專屬邏輯全部留在 router
  端處理。
- **出站檔案走 `outbox://` 佔位字串**（見 `docs/file-share-design.md`）：LINE 不允許
  bot 傳檔案，所以 agent 要交檔案給使用者時呼叫 `share_file` 工具，工具把檔案複製到
  `$HERMES_HOME/outbox/<token>/<檔名>`（router 端的 `data/<room_id>/outbox/…`）並回傳
  佔位字串 `outbox://<token>`，agent 原樣貼進回覆。router 在 `core._take_turn` 送出前
  （`file_links.publish_file_links`）驗證那個目錄裡恰好一個一般檔（`O_NOFOLLOW` +
  `fstat` + 大小上限）、複製到房間 mount 之外的 `data/_files/<room_id>/<token>/`、刪掉
  outbox 那份，最後把佔位字串換成 `{PUBLIC_BASE_URL}/files/<room_id>/<token>`。
  檔案系統是這條路的唯一介面：**容器不需要知道自己的 room_id 或 router 的公開網址，
  所以沒有新增任何容器 env，既有房間不必重建**；沒設 `PUBLIC_BASE_URL` 時 router 把
  佔位字串換成一句「未設定」提示，容器端一樣不知情。
- **媒體走檔案系統、不走 API body**：避免疊床架屋改用 base64 多模態，也繞開了
  `api_server` 本身不支援 file/audio/video content part 的限制。
