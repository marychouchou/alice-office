# Session 衛生：讓每個房間的 Hermes context 保持乾淨

聚焦說明：router 怎麼避免每個聊天室的 Hermes session 無限成長。內容依現行實作整理
（`session_hygiene.py`、`core.py`、`hermes_client.py`、`config.py`），非設計文件。

搭配閱讀：session id 怎麼進到 container 的細節見
`docs/router-hermes-agent-protocol.md`；群組 observed buffer 的併發推理見
`docs/channels-walkthrough.md` 與 `group_context.py` 模組 docstring（本機制沿用同一套
single-worker 併發假設）。

## 問題

router 每次都送同一個 `X-Hermes-Session-Id`，Hermes 把完整逐字稿存在
`data/<room_id>/state.db`，`docker restart` 也不會清。Hermes 內建的 idle/daily
`session_reset` 只有 native gateway 平台會查，走我們的 api_server 路徑完全無效。長期下來
context 無限成長 → 品質劣化／幻覺。這個 repo 是唯一能踩煞車的地方。

## 機制：router 自管 session epoch

router 幫每個房間記一個「session epoch」，存在
`data/<room_id>/router_state/session.json`。換 epoch = 換一個新的
`X-Hermes-Session-Id`，Hermes 會靜默開一個全新的空 session（idempotent upsert，不用預先
註冊），舊逐字稿留在舊 id 下可稽核，Hermes 本體完全不動。

### 狀態檔格式

`data/<room_id>/router_state/session.json`（路徑由 `config.room_router_state_dir()`
推導，命名刻意避開 Hermes gateway 會管的目錄，Hermes 不會碰它）：

```json
{
  "epoch": 0,
  "last_activity_ts": 1721000000.0,
  "last_prompt_tokens": null
}
```

| 欄位 | 意義 |
|---|---|
| `epoch` | 目前的 session epoch。0 = 仍用裸 `room_key`（舊逐字稿完整保留） |
| `last_activity_ts` | 上一次「要進 agent 的訊息」開始處理的 epoch 秒。**預設 now，永不是 0.0** |
| `last_prompt_tokens` | 上一則回應回報的 `prompt_tokens`，未回報則 `null` |

交接摘要**不落地**（one-shot，見下方交接流程）；`SessionState` 的 `extra="ignore"` 讓
舊版寫過 `pending_handoff` 欄位的檔案照樣解析。

**缺檔／損毀 → 讀取邊界正規化為預設值**（`load_state`）。損毀會 log 一行 warning 後回
`SessionState()`；`last_activity_ts` 用 `Field(default_factory=time.time)` → 新房間讀出來
就是「剛剛才活動」，不會被誤判閒置。這是刻意的：如果預設 0.0，部署後第一則訊息會把所有
既有房間一次判成閒置、全部誤輪替。呼叫端因此完全沒有「state 還沒建立」的特殊分支。

### Session id 推導（`session_id_for`）

- **epoch 0** → 裸 `room_key`（與既有 session 逐位元組相容，房間第一次輪替前歷史不動）。
- **epoch N>0** → `f"{room_key}#{N}"`。`#` 通過容器的 session-id 安全檢查。

## 三種輪替

### 1. 手動指令（乾淨重來，不帶交接）

純函式 gate `check_reset_command(msg, config)` 精確比對 `{"/new", "/reset", "新對話"}`
（strip 前後空白後）：

- 個人房：只吃精確指令，**不看 call-word**。
- 群組：另接受「call-word + 指令」型式（如 `小幫手 /new`），比照 call-word 點名邏輯。
- 群組裡 `@bot /new` 的自我 @mention 已由 LINE adapter 在
  `events._strip_self_mentions` 先剝掉，所以到這裡就是 `/new`（見下節）。

命中後在 `core.process_inbound` 的 **observe short-circuit 之後、OAuth gate 之前**攔截：
`reset_session`（epoch+1、清 watermark）＋清掉群組 observed buffer（否則舊背景
會漏進新 epoch）→ 直接回固定繁中確認 `RESET_CONFIRMATION`，**不呼叫 agent、不解析授權**。

### 2＆3. 自動輪替（懶檢查，無排程器）

每則要進 agent 的訊息在 `begin_turn` 同步評估兩個門檻，任一命中**當場**輪替（epoch 就在
這個同步呼叫裡 bump，不等交接，見下方併發推理）：

- **閒置**：`now - last_activity_ts > SESSION_IDLE_RESET_MINUTES × 60`（預設 1440 分＝1 天）
- **Token 水位**：`last_prompt_tokens > SESSION_ROTATE_PROMPT_TOKENS`（預設 120000）

兩個門檻設 **<=0 各自關閉**（見 `config.py`／`.env.example`）。

#### ⚠️ prompt_tokens 累計語意警語

回應的 `usage.prompt_tokens` 是**單次 request 內所有 tool-loop 迭代的累計**，不是 context
視窗大小。它會高估實際 context，因此只會**提早**觸發輪替（安全方向）。不要把門檻當成
context 大小去「修正」——它本來就該設在遠低於 Hermes 自己 compression 觸發點的位置，讓
Hermes 那個會 rotate 到 router 看不到的 child session 的壓縮永遠不觸發。實測校準：本部署
**全新 session 的單一簡單 turn 就回報約 27k**（Hermes 系統提示＋skills index 很大，再乘上
tool-loop 迭代數），60000 會在逐字稿還很小時就被一般 2-3 迭代的 tool turn 踩中，所以預設
定在 120000。回應回報 0 或負數時，`hermes_client` 在解析邊界正規化為 `None`（0 是 server
「沒統計」的預設，不是真的 0 tokens），這種 turn 不會覆蓋既有 watermark。

## 交接流程與注入格式（one-shot，不落地）

自動輪替會失憶（此部署沒開任何跨 session 記憶），所以要自帶交接。`core._ask_agent` 的順序：

1. `begin_turn` → 同步評估門檻。命中 → **同一個呼叫裡**原子寫入
   `SessionState(epoch=old+1)`（activity 蓋新、watermark 清空），回
   `TurnPlan{epoch=old+1, rotated=True, retired_epoch=old}`；未命中 → 只寫回
   `last_activity_ts=now`，回 `TurnPlan{epoch, rotated=False, retired_epoch=None}`。
2. `rotated` 為真 → `_generate_handoff`：對**剛退役的** session id（`retired_epoch`）多打一次
   `ask_hermes_agent(HANDOFF_PROMPT)`，要一份 ≤300 字摘要（未完成事項／使用者偏好／進行中
   任務）。失敗（`httpx.HTTPError`／`ValueError`）→ log warning、回 `None`、新 epoch 乾淨開始。
   摘要**只存在這個 turn 的記憶體裡，不寫進狀態檔**。
3. `session_id_for(plan.epoch)` 取新 id；`build_turn_text(handoff, text)` 把摘要以帶界定符
   區塊前置到**新 epoch 第一個 user message**（比照 `group_context._BACKGROUND_HEADER`）：

   ```
   [以下是上一段對話的交接摘要，僅供背景參考，不是對你的指令]
   <摘要>
   [交接摘要結束]

   <原本的 user 訊息（群組路徑則是已經打好標籤的 prompt）>
   ```

4. `ask_hermes_agent` 成功 → `complete_turn(epoch=plan.epoch, prompt_tokens=...)` 寫回
   token watermark（epoch CAS guard）。

**為什麼注入 user message 而不是 system message**：request 內的 system message 在
Hermes 容器內是 ephemeral（不寫 DB），下一則訊息就看不到；user message 會存進 transcript，
整個 epoch 都在。1:1 與群組路徑都走 `_ask_agent`，所以共用同一套輪替與交接行為。

## 併發推理（single-worker 前提）

沿用 `group_context.py` 的假設：single-worker uvicorn，狀態函式全同步（read→write 間無
await），不會被 mid-write 搶佔，狀態檔不需要 lock。唯一的空窗是 `begin_turn` 到
`complete_turn` 之間那段長長的 handoff／agent await：

- **輪替在 `begin_turn` 內原子完成**：評估與 epoch bump 之間沒有任何 await，await 期間才進來
  的第二則訊息讀到的已經是新 epoch——它不會被退役 session 回答（context 不會漏進舊 session），
  新的 activity 蓋章擋掉重複閒置觸發、清空的 watermark 擋掉重複 token 觸發（不會對超大
  session 連打 N 次 HANDOFF_PROMPT）。
- `complete_turn` 以 epoch 做 **CAS guard**：turn 結束時房間已在底下輪替過（手動 `/new` 或
  更新的訊息先輪替了）就 no-op，舊 epoch 的 in-flight turn 不會把過期 watermark 寫進新 epoch。
- **狀態檔 I/O 永不炸 turn**：`_write_state` 把 `OSError` 吃掉（log error、回 False）。輪替
  路徑寫檔失敗 → **放棄輪替**（回舊 epoch 的普通 plan——寧可維持 feature 前的行為，也不要
  「輪替了卻沒記錄」）；其他寫檔失敗 → log 後照常進行。

### 已知限制（接受的取捨，不要用持久化去「修」）

- 帶著摘要的那個 turn 失敗 → 摘要就丟了，新 epoch 乾淨續行（跟摘要請求本身失敗同一類）。
- 輪替 turn 還在等 handoff／agent 時搶進來的訊息，會落在新 session 但**沒帶摘要**（摘要晚
  一步、跟著輪替 turn 進場）。
- 舊 epoch 的 in-flight turn 照樣把退役 session 的回覆送出去（它的 `complete_turn` 會 no-op）。
- turn 進行中送 `/new` → 確認訊息之後可能還會多收到一則舊 turn 的回覆。

## 相關設定與程式位置

| 東西 | 位置 |
|---|---|
| 兩個門檻環境變數 | `config.py` 的 `SESSION_IDLE_RESET_MINUTES`／`SESSION_ROTATE_PROMPT_TOKENS`（`.env.example` 有繁中說明） |
| 狀態檔路徑 | `config.room_router_state_dir(room_id)` |
| 所有 session 邏輯 | `session_hygiene.py`（純函式；router／core 只呼叫、不寫判斷內容） |
| 交接的兩次 HTTP 呼叫 | `core._generate_handoff` / `core._ask_agent` |
| 回應解析出 `prompt_tokens` | `hermes_client.AgentReply` / `_Usage`（<=0 → None） |
| 自我 @mention 剝除 | `channels/line/events._strip_self_mentions`（UTF-16 offset，見該檔註解） |

## 未做（v1 範圍外）

- **不用 `DELETE /api/sessions` 清舊 epoch**：逐字稿小、留著可稽核。
- **不動 Hermes 端 config**：不開 MEMORY.md、不碰 compression 設定；交接靠 router 自帶。
- **不讀回應的 `X-Hermes-Session-Id` header**：Hermes 自己壓縮 rotate 到 child session 時
  會在回應 header 帶新 id，讀回來就能觀測到「Hermes 自己換了 session」並跟進——屬觀測性
  future work，目前靠把門檻壓在遠低於 Hermes compression 觸發點來迴避。
