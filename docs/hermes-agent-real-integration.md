# 從 Mock Hermes Agent 換成真實 Hermes Agent — 變更紀錄

> 取代文件：`docs/hermes-agent-test.md` 測試的是舊的 `hermes-agent:local`（一支手寫的 143 行
> FastAPI mock），已經不是目前的架構，內容已過時。

## 為什麼改動這麼大

一開始以為 `hermes-agent:local` 就是「Hermes Agent」，但實際拆開這個 image 才發現它是之前手寫
的一支最小可行原型（`main.py`，143 行）：收到 LINE webhook → 把技能文字塞進 prompt 呼叫一次
OpenAI → 解析 `<create_skill>` 標籤存檔 → push 回 LINE。沒有真正的技能執行引擎、沒有程式碼執行
能力、也沒有對話記憶，只是拿來驗證「router 能不能動態生成 container」這件事的假貨。

同時我們手上有一個真的 `nousresearch/hermes-agent` image（也就是 `hermes-test` 那個 container），
是一支功能完整的 AI agent（有自己的 skills 系統、cron、kanban、multi-platform gateway 等），但它
內建的 LINE 整合方式（自己起 webhook server、自己驗簽、自己 push）跟這個專案原本 `CLAUDE.local.md`
設計書假設的完全不同。

最後確認的正確架構是：**router 自己擁有 LINE 的進出**（驗簽、收訊息、push 回覆），Hermes
container 只是被動的「大腦」——透過它內建的 `api_server` platform（OpenAI-compatible API）純粹
收文字、吐文字。這跟原本假設「container 自己接 LINE webhook」的模型完全不同，所以三個核心檔案
（`config.py`、`container_manager.py`、`router.py`）都要重寫，而不是小修小補。

## 架構總覽

```
LINE 平台
   │ webhook POST（router 驗簽）
   ▼
alice-office-router (FastAPI)
   │ 1. 解出 room_id + 文字
   │ 2. get_or_create_container(room_id)  → 沒有就用 nousresearch/hermes-agent 生成
   │ 3. POST {container}/v1/chat/completions  (Bearer API_SERVER_KEY)
   │ 4. 拿到回覆文字
   │ 5. push_line_message()  → 呼叫 LINE Push API 把回覆推回聊天室
   ▼
Hermes Agent container（每個 room 一個，/opt/data 掛載host目錄，硬隔離）
```

Hermes container 完全不碰 LINE、不需要 `LINE_CHANNEL_*` 環境變數；router 完全不用 Hermes 內建
的 LINE plugin。

## 檔案異動

| 檔案 | 異動內容 |
|---|---|
| `src/alice_office_router/config.py` | `HERMES_IMAGE` 改成 `nousresearch/hermes-agent`；新增必填的 `HERMES_API_SERVER_KEY`（router 與所有 container 共用的 Bearer 密鑰） |
| `src/alice_office_router/container_manager.py` | 掛載路徑 `/root/.hermes` → `/opt/data`；啟動 command 固定帶 `gateway run`；環境變數不再傳 LINE 憑證，改傳 `API_SERVER_KEY`、`API_SERVER_HOST=0.0.0.0`、`LLM_API_KEY`；新增 `_ensure_config_yaml()` 自動在新房間目錄寫入 provider 設定；新增 `_wait_until_ready()` 輪詢 `/health`，取代原本寫死的 `sleep(2)` |
| `src/alice_office_router/hermes_client.py`（新增） | 呼叫 container 的 `/v1/chat/completions`，帶 `X-Hermes-Session-Id: room_id` 讓同房間對話有記憶延續性 |
| `src/alice_office_router/line_client.py`（新增） | 用既有的 `line-bot-sdk` 依賴呼叫 LINE Push Message API |
| `src/alice_office_router/router.py` | webhook handler 改成：驗簽 → 取文字 → 背景任務跑「取得 container → 問 Hermes → push 回 LINE」三步驟，各自獨立 try/except + log |
| `pyproject.toml` | ruff 排除 `data/`（container 執行期產生的技能檔案，非專案原始碼）；mypy 對 `linebot.*` 忽略缺 stub 的警告 |
| `.env` | `HERMES_IMAGE` 修正；新增隨機產生的 `HERMES_API_SERVER_KEY` |
| `tests/` | 對應更新既有測試 + 新增 `test_hermes_client.py`、`test_line_client.py`，共 27 個測試 |
| `scripts/test_webhook.py` | log 判讀提示更新（不再找 container log 裡的 `/line/webhook`，改找 `/v1/chat/completions`；LINE push 結果要看 router 自己的 log） |

## 測試中發現的真實 bug

第一次真跑整條路時，container 建立後只 `sleep(2)` 就假設它 ready 並開始打 API，結果被
`Server disconnected without sending a response` 拒絕——因為真正的 Hermes gateway 要跑完
s6 supervision、技能同步、multi-stage 啟動，遠比 mock 慢。修法：`_wait_until_ready()` 改成輪詢
`GET /health` 直到 200 或 60 秒逾時才罷休。

## End-to-end 驗證結果（真實 docker + 真實 vLLM + 真實 LINE API，共測 3 輪）

```
POST /webhook                        → 200
建立 hermes_<room_id> container
GET  /health                         → 200（輪詢等到 ready）
POST /v1/chat/completions            → 200
   回覆內容：「我是 Hermes Agent，由 Nous Research 所開發的智能 AI 助手，
              專為協助用戶完成各類任務而設計。」
POST LINE Push Message API           → 400 "'to' 屬性無效"
   （預期行為：測試用的 room_id 不是真實 LINE ID，但 400 而非 401
    證明 token、簽章、呼叫格式都正確，已經真的打到 LINE 正式 API）
```

另外驗證了同房間對話記憶：追問「我剛剛問你什麼？」，agent 正確覆述第一則訊息。

## 已知事項 / 待辦

- Hermes container 啟動時會印一則安全警告：`api_server` 綁定 `0.0.0.0` 且 `terminal.backend`
  是 unsandboxed 的 `local`，代表透過這個 API 派送的 agent 工作是以 host user 身份、有完整
  終端機/檔案存取權限在跑。目前每個房間各自一個 container 已經有 docker 層級隔離，但如果要更
  保守，可以考慮把 Hermes 內部的 `terminal.backend` 設成 `docker`（agent 自己再包一層 sandbox）。
- 尚未對「真實 LINE 使用者」推播成功做最終確認（受限於測試環境沒有真實 userId/roomId）；管線
  本身（簽章、認證、呼叫格式）已透過真實 LINE API 的結構化錯誤回應證實無誤。
- `docs/hermes-agent-test.md` 是舊架構的測試指南，已經過時，之後可以刪除或整份改寫。
