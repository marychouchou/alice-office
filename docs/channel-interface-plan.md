# Channel Interface 實作計畫

日期：2026-07-14
設計依據：[channel-interface-design.md](channel-interface-design.md)

每個 Phase 是一個獨立可 commit 的單位（conventional commits），commit 前必跑
`uv run ruff check . && uv run mypy src/ && uv run pytest`。
Phase 之間有嚴格順序依賴，不可並行。每個 Phase 的範圍都夠小，適合各交給一個
subagent 執行（planning 已完成於本文件；執行可用 sonnet 5 / opus 4.8）。

## Phase 1 — 抽出 channel-free core（行為不變）

**目標**：`router.py` 裡的泛用編排邏輯移到 `core.py`，LINE 依賴留在原地。

1. 新增 `channels/base.py`：`InboundMessage` model（`channel`／`room_key`／`text`；
   `ChannelAdapter` Protocol 可留到 Phase 2 再加）。同時刪掉殘留的
   `src/alice_office_router/channels/__pycache__/`。
2. 新增 `core.py`：`async def process_inbound(msg: InboundMessage, config: Settings) -> list[str]`。
   把 `router.py` 的 `_apply_google_gate` + `_process_and_reply` 邏輯搬進來：
   gate blocked → 回傳 `[blocked_msg]`（不呼叫 agent）；notice → 回傳
   `[notice_msg, agent_reply]`；正常 → `[agent_reply]`。
   函式內不 import 任何 `linebot`／LINE 模組，不接觸 reply_token。
3. `router.py` 改為：解析 webhook → 組 `InboundMessage`（room_key 此階段仍是
   裸 LINE id，前綴留到 Phase 3）→ `texts = await process_inbound(...)` →
   逐則走既有 `_deliver_reply`（第一則用 reply token，其餘 push）。
4. **已知行為差異（寫進 commit message）**：gate notice 原本在 agent 回覆前
   先 push，現在改為與 agent 回覆一起在處理完成後送出。
5. 測試：`test_router.py` 中 gate／agent 編排的測試搬成 `tests/test_core.py`
   （mock `container_manager`、`hermes_client`、`check_google_authorization`）；
   wire-format 測試留在 `test_router.py`。
6. 驗收：三件套綠；真機 LINE round-trip（`scripts/test_webhook.py` 或實機傳訊）。

Commit: `refactor: extract channel-free core.process_inbound from router`

## Phase 2 — channels/ 目錄結構（搬家 + LINE adapter 化）

**目標**：LINE 檔案群收進 `channels/line/`，router 消失成 adapter。

1. `git mv`（保留歷史）：
   - `line_verify.py` → `channels/line/verify.py`
   - `line_events.py` → `channels/line/events.py`
   - `line_format.py` → `channels/line/format.py`
   - `line_client.py` → `channels/line/client.py`
   - `line_dedup.py` → `channels/line/dedup.py`
2. `router.py` → `channels/line/adapter.py`：`LineAdapter` class，
   `name = "line"`、`api_router()` 回傳含 webhook handler 的 `APIRouter`、
   私有 `_deliver()`（reply-token-優先-push-兜底，含 `format_for_line` 切塊）。
3. `channels/__init__.py`：`enabled_adapters(config) -> list[ChannelAdapter]`
   （目前只回 `[LineAdapter(config)]`）。
4. `main.py`：迴圈 mount 每個 adapter 於 `/webhooks/{name}`；
   **`/webhook` 保留為 alias 指到 LINE handler**（LINE console 不用即刻改），
   在 alias 旁註明棄用條件：LINE console 改指 `/webhooks/line` 後移除。
5. 測試搬家：`tests/channels/line/{test_verify,test_events,test_format,test_client,test_dedup,test_adapter}.py`；
   `test_adapter.py` = 原 `test_router.py` 的 wire-format 部分（含 `/webhook`
   alias 與 `/webhooks/line` 都要打）。`conftest.py` 的簽章 helper 跟著調整。
6. 文件同步（grep 舊路徑 `line_events`、`line_format`、`router.py`…）：
   - **CLAUDE.md**：Architecture 圖、路由表「LINE wire format → `src/alice_office_router/line_*.py`」
     改為 `channels/line/`、反模式 6／9 的檔案路徑、新增一列
     「新通訊通道 → `channels/<name>/`，core 只認 `InboundMessage`」。
   - `docs/troubleshooting.md`、`docs/line-hermes-message-flow.md` 等提及舊檔名處。
7. 驗收：三件套綠；真機 LINE round-trip（兩個路徑都測）。

Commit: `refactor: move LINE wire format into channels/line adapter`

## Phase 3 — room_key 前綴 + 一次性遷移

**目標**：房間鍵改為 `line_<native_id>`，消除未來的跨通道撞名與永久特殊情況。

1. `channels/line/events.py`：`Source.room_id` 出口處改產 `room_key = f"line_{native_id}"`；
   native id（push／blob API 用）只在 adapter 內部換算
   `room_key.removeprefix("line_")`。
2. 下游（容器名、`data/<room_key>/`、Google `account_key`）自動跟隨，理論上零改動；
   grep 驗證沒有其他地方自己拼裸 id。
3. `hermes_client.py`：`X-Hermes-Session-Id` → `X-Hermes-Session-Key`。
   **實作前先真機驗證** hermes `api_server` 接受此 header（起一個容器直接 curl）；
   若不接受，維持原 header 並在本文件記錄原因。
4. 新增 `scripts/migrate_room_keys.py`（**預設 dry-run**，`--apply` 才動手）：
   - 掃 `data/` 下符合 `^[UCR][0-9a-f]{32}$` 的目錄 → 改名 `line_<id>`。
   - `docker rm -f hermes_<舊id>`（下次訊息自動重建）。
   - 改寫房間內 seeded 檔中的舊 id 字串：至少 `config.yaml` 與 MCP manifest
     的 `{room_id}`／`{account_key}` 代入處——**實作時先 grep 一個實際房間目錄
     確認完整清單**，逐檔文字替換 `舊id → line_舊id`、`舊id小寫 → line_舊id小寫`。
   - Google `tokens.json`：key `<舊id小寫>` → `line_<舊id小寫>`。
   - 冪等：已是 `line_` 前綴的目錄跳過；每步印出動作。
5. 測試：既有測試的 room_id fixture 改用 `line_U...` 形式；migration script
   加 `tests/test_migrate_room_keys.py`（tmp dir 上跑 dry-run／apply）。
6. 已知影響（寫進 commit message）：session key 改變 + 遷移 → 各房間對話
   從新 session 開始（舊 transcript 檔案仍在房間資料夾內）。
7. 驗收：三件套綠；對現有 `data/` 先 dry-run 檢視輸出再 `--apply`；
   真機 LINE round-trip；有 Google 授權的房間驗證 gate 仍判定已授權
   （tokens.json key 遷移正確）。

Commit: `feat: namespace room keys by channel (line_*) with one-shot migration`

> **2026-07-14 實機驗證結果（item 3，決定：維持 `X-Hermes-Session-Id`）**
> 對 `HERMES_IMAGE=alice-hermes-agent:local` 內的
> `gateway/platforms/api_server.py` 原始碼做靜態檢查（當下無執行中的
> `hermes_*` 容器，起容器 curl 太重，改讀 image 內原始碼——語意權威來源相同）。
> 發現 `/v1/chat/completions` 兩個 header 的語意其實**不同**：
> - `X-Hermes-Session-Id`：session continuity——**有帶才會從 `state.db`
>   載入該 session 的歷史對話**（`_parse` 後 `db.get_messages_as_conversation`）。
>   本 router 每則訊息只送單一則 user message、不送歷史，所以「跨訊息記得上下文」
>   完全靠這個 header。
> - `X-Hermes-Session-Key`：只做 **long-term memory（Honcho）scoping**，
>   與 Session-Id 獨立，`/new` 後仍穩定；但**它本身不會載入 transcript 歷史**。
>
> 也就是說 Session-Key 雖被 api_server 接受，但它**不是** `/v1/chat/completions`
> 的「穩定對話身分」——若改成只送 Session-Key 並拿掉 Session-Id，歷史載入分支
> 不會執行，每則訊息都會失憶（continuity 退化）。因此落在計畫的「不接受／語意
> 不符 → 維持原 header」分支：**保留 `X-Hermes-Session-Id`**。
> 其 header 值本來就已是 `room_key`（core 傳 `msg.room_key`），Phase 3 後自然變成
> `line_<id>`，每房間仍是穩定且隔離的 session（各房間各自一個容器）。已知影響
> （item 6）維持成立：值從 `U…` 變 `line_U…`，等於各房間從新 session 開始。
> 設計文件 §7 的前提（Session-Id 會隨 `/new` 輪替）在本部署不成立——router 從不
> 觸發 `/new`／`/reset`，Session-Id 事實上恆定，故無需改用 Session-Key。

## Phase 4 — 第一方 API 通道（TUI／mobile／dev）

**目標**：不經 LINE 就能打進任何房間；TUI 與 mobile 的正式入口。

1. `config.py`：`API_CHANNEL_TOKEN: str | None = None`。
2. `channels/api.py`：`ApiChannelAdapter`（`name = "api"`）：
   - `POST /webhooks/api/messages`，`Authorization: Bearer` 驗證
     （`hmac.compare_digest`），失敗回 401。
   - body `{"room_key": str, "text": str}`（pydantic 驗證；`room_key` 允許
     既有任何通道的 key，或 `api_[a-z0-9-]{1,32}` 的自有房間）。
   - 呼叫 `core.process_inbound` → 同步回 `{"replies": [...]}`（原始 markdown，
     不做 LINE 式剝除／切塊）。
3. `channels/__init__.py`：`API_CHANNEL_TOKEN` 有值才把 adapter 加進清單。
4. 測試 `tests/channels/test_api.py`：401、422、正常流（mock core）、
   token 未設定時路由不存在。
5. 文件：README 與 `docs/troubleshooting.md` 加 curl 範例
   （對既有 `line_*` 房間發訊除錯、對 `api_*` 房間開新對話）。
6. 驗收：三件套綠；真機 curl 對既有 LINE 房間打一則、收到 agent 回覆；
   對 `api_dev` 房間打一則、確認新容器 `hermes_api_dev` 建立。

Commit: `feat: add first-party API channel for TUI/mobile/dev access`

## Phase 5 —（延後，不在本輪範圍）

| 項目 | 觸發條件 |
|---|---|
| `channels/telegram/` | 真的要接 Telegram 時；屆時允許修 `base.py`（介面第一次被第二個 webhook 通道驗證） |
| Protocol 加 `send_text()` | 出現主動推播需求（cron 提醒、agent 主動發話、mobile notification） |
| TUI 本體 | API 通道就緒後另開 task，TUI 是獨立 client 專案不進本 repo 的 src |
| 移除 `/webhook` alias | LINE console 改指 `/webhooks/line` 之後 |

## 風險與回退

- Phase 1–2 是行為保持的重構，回退 = revert commit。
- Phase 3 動到磁碟資料：migration script 預設 dry-run；`--apply` 前手動備份
  `data/`（`cp -a data data.bak-<date>`）。回退 = 還原備份 + revert commit。
- 全程 LINE 服務不中斷需求：每個 Phase 結尾都有真機 round-trip 驗收，
  部署以 Phase 為單位。
