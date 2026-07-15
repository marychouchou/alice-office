# C4 架構圖 — Alice Office Router

依 2026-07-15 的程式碼現況（channel adapter 重構 + 第一方 API channel 已落地）繪製。
三個層級：System Context → Container → Component；Class 層級暫不畫。

> **名詞澄清**：C4 的「Container」指的是「可獨立部署/執行的單元」，不等於 Docker
> container——只是在這個系統裡兩者剛好高度重合（router 是一個 process、每個房間的
> Hermes agent 是一個 Docker container）。

圖使用 Mermaid flowchart 語法搭配 C4 慣例配色（深藍＝人、藍＝系統內元素、灰＝外部
系統），GitHub 網頁與 VS Code Markdown 預覽可直接渲染。（不用 Mermaid 原生 C4
語法是因為其排版引擎會讓標籤重疊。）

---

## Level 1 — System Context

系統邊界是「Alice Office」整體：router 加上所有房間的 Hermes agent 容器。
使用者只透過 LINE（或自家 client）互動，感受上就像直接使用自己部署的 Hermes agent。

```mermaid
flowchart TB
  classDef person fill:#08427b,color:#fff,stroke:#052e56
  classDef system fill:#1168bd,color:#fff,stroke:#0b4884
  classDef ext fill:#999999,color:#fff,stroke:#6b6b6b

  employee["👤 企業使用者<br/><i>在 LINE 聊天室與自己房間的 AI 助理對話；<br/>一個聊天室 = 一個隔離的 agent</i>"]:::person
  dev["👤 開發者 / 自家 client<br/><i>TUI、mobile、curl 除錯，走第一方 API channel</i>"]:::person
  alice["<b>Alice Office</b><br/><i>LINE OA webhook router +<br/>每房間一個 Hermes agent 容器（房間即隔離邊界）</i>"]:::system
  line["LINE Platform<br/><i>Messaging API：webhook 推送、<br/>Reply/Push、媒體內容下載</i>"]:::ext
  google["Google<br/><i>OAuth 2.0 授權；Gmail / Drive API</i>"]:::ext
  llm["LLM Provider<br/><i>OpenAI 相容推理端點<br/>（LLM_BASE_URL / LLM_MODEL）</i>"]:::ext

  employee -- "傳訊息（LINE app）" --> line
  line -- "Webhook 事件（HTTPS POST）" --> alice
  alice -- "Reply / Push 回覆、下載媒體" --> line
  dev -- "直接對話 / 除錯（HTTPS + Bearer token）" --> alice
  employee -- "瀏覽器開啟 OAuth 授權連結<br/>（/oauth/start → callback）" --> alice
  alice -- "OAuth token 交換；<br/>Gmail / Drive API 呼叫" --> google
  alice -- "chat completions" --> llm
```

---

## Level 2 — Container

系統內三種可部署/執行單元：router（FastAPI process）、每房間一個的 Hermes agent
Docker container、以及每房間一份的資料目錄（bind mount，等同房間的持久化儲存）。
Docker Engine 視為部署環境的外部依賴——router 透過 docker SDK 動態建立房間容器。

```mermaid
flowchart TB
  classDef person fill:#08427b,color:#fff,stroke:#052e56
  classDef container fill:#1168bd,color:#fff,stroke:#0b4884
  classDef db fill:#1168bd,color:#fff,stroke:#0b4884
  classDef ext fill:#999999,color:#fff,stroke:#6b6b6b

  employee["👤 企業使用者<br/><i>LINE 聊天室</i>"]:::person
  dev["👤 開發者 / 自家 client<br/><i>TUI / mobile / curl</i>"]:::person
  line["LINE Platform<br/><i>Messaging API</i>"]:::ext
  google["Google<br/><i>OAuth 2.0 + Gmail / Drive API</i>"]:::ext
  llm["LLM Provider<br/><i>OpenAI 相容端點</i>"]:::ext
  docker["Docker Engine<br/><i>同主機；hermes_global_net 網路</i>"]:::ext

  subgraph alice["Alice Office"]
    router["<b>Alice Office Router</b><br/><i>Python 3.12 / FastAPI</i><br/>驗簽、解析各 channel wire format、事件 dedup、<br/>Google OAuth gate、依 room_key 分派到房間容器、<br/>把回覆送回房間"]:::container
    hermes["<b>Hermes Agent 容器（每房間一個）</b><br/><i>Docker：hermes_&lt;room_key&gt;，port 8642</i><br/>hermes-agent gateway + 該房間自己的<br/>MCP servers / plugins / skills；容器間互不相通"]:::container
    roomdata[("<b>房間資料 data/&lt;room_key&gt;/</b><br/><i>host 目錄 bind mount → /opt/data（HERMES_HOME）</i><br/>sessions、skills、kanban.db、state.db、config.yaml、<br/>mcp/、plugins/、Google tokens——每房間各自一份")]:::db
  end

  employee -- "傳訊息（LINE app）" --> line
  line -- "POST /webhooks/line（舊 /webhook 保留）<br/>HMAC-SHA256 驗簽" --> router
  router -- "Reply → Push fallback、下載媒體<br/>（linebot SDK）" --> line
  dev -- "POST /webhooks/api/messages<br/>（Bearer token）" --> router
  employee -- "/oauth/start、/oauth/callback<br/>（瀏覽器）" --> router
  router -- "OAuth 2.0 code ↔ token 交換" --> google
  router -- "建立 / 啟動 / 查詢 hermes_&lt;room_key&gt;<br/>（docker SDK，只在 container_manager.py）" --> docker
  router -- "POST /v1/chat/completions<br/>（session id = room_key，HERMES_API_SERVER_KEY）" --> hermes
  router -- "write-once seed（config.yaml、mcp/、plugins/）<br/>tokens.json 讀寫" --> roomdata
  hermes -- "HERMES_HOME 讀寫；<br/>每次開機自行補齊 sessions / skills / db" --> roomdata
  hermes -- "chat completions" --> llm
  hermes -- "Gmail / Drive MCP 以房間 token 呼叫" --> google
```

責任分界（誰寫 `data/<room_key>/` 的哪部分）：

| 寫入者 | 內容 | 時機 |
|---|---|---|
| Router（container_manager） | `config.yaml`、`mcp/`、`plugins/` seed | 房間第一次建立，write-once，之後永不覆蓋 |
| Router（google_oauth） | Google `tokens.json` | OAuth callback / refresh |
| Hermes gateway | `sessions/`、`skills/`、`kanban.db`、`state.db`、`logs/`、lock 檔 | 每次容器開機自行補齊與執行期寫入 |

---

## Level 3 — Component：Alice Office Router

Router 內部的元件與訊息路徑。核心設計：channel adapter 各自擁有自己的 wire format
（驗簽、解析、dedup、送訊），唯一出口是 channel-free 的 `core.process_inbound`；
core 只認 `InboundMessage`（channel + room_key + 純文字），回傳 `list[str]`，
從不碰任何 channel 的送訊 API。

```mermaid
flowchart TB
  classDef comp fill:#438dd5,color:#fff,stroke:#2e6295
  classDef ext fill:#999999,color:#fff,stroke:#6b6b6b
  classDef container fill:#1168bd,color:#fff,stroke:#0b4884

  line["LINE Platform"]:::ext
  google["Google OAuth"]:::ext
  docker["Docker Engine"]:::ext
  hermes["Hermes Agent 容器<br/><i>hermes_&lt;room_key&gt;:8642</i>"]:::container
  roomdata[("data/&lt;room_key&gt;/")]:::container

  subgraph router["Alice Office Router（FastAPI app）"]
    main["<b>main</b><br/>FastAPI app 組裝：enabled_adapters 掛到<br/>/webhooks/&lt;name&gt;（LINE 另掛舊 /webhook）＋ oauth_router"]:::comp
    registry["<b>channels.enabled_adapters</b><br/>靜態 registry：LINE 恆啟用；<br/>API channel 依 API_CHANNEL_TOKEN 決定"]:::comp
    line_adapter["<b>channels.line — LineAdapter</b><br/>verify（HMAC 驗簽）｜events（wire format 解析＋<br/>媒體/貼圖/位置→佔位文字）｜dedup（事件去重）｜<br/>client（Reply → Push fallback）｜format（長度/則數切分）"]:::comp
    api_adapter["<b>channels.api — ApiChannelAdapter</b><br/>Bearer 驗證；room_key 形狀白名單<br/>（line_* / api_*）；同步回傳原始 markdown"]:::comp
    core["<b>core.process_inbound</b><br/>channel-free：gate → 容器 → agent → list[str]<br/>不碰任何 channel 的送訊 API"]:::comp
    oauth["<b>google_oauth</b><br/>check_google_authorization 純函式 gate；<br/>/oauth/start、/oauth/callback；tokens.json 存取"]:::comp
    cm["<b>container_manager</b><br/>get_or_create_container：docker 生命週期＋<br/>write-once seed＋config.yaml 渲染"]:::comp
    hc["<b>hermes_client</b><br/>ask_hermes_agent：POST /v1/chat/completions，<br/>session id = room_key 維持對話連續性"]:::comp
  end

  main -- "啟動時取得啟用的 adapters" --> registry
  registry -- "建立並掛載" --> line_adapter
  registry -- "建立並掛載（有 token 才啟用）" --> api_adapter
  line -- "Webhook POST" --> line_adapter
  line_adapter -- "Reply / Push、媒體下載" --> line
  line_adapter -- "InboundMessage" --> core
  api_adapter -- "InboundMessage" --> core
  core -- "check_google_authorization(room_key)<br/>→ blocked / notice / ok" --> oauth
  core -- "get_or_create_container(room_key)<br/>→ 容器 URL" --> cm
  core -- "ask_hermes_agent(url, room_key, text)" --> hc
  cm -- "docker SDK" --> docker
  cm -- "write-once seed" --> roomdata
  oauth -- "code ↔ token 交換" --> google
  oauth -- "tokens.json 讀寫" --> roomdata
  hc -- "POST /v1/chat/completions" --> hermes
```

圖上刻意省略的兩個橫切元件（畫成箭頭會變蜘蛛網）：

- **`channels.base`** — `InboundMessage` model 與 `ChannelAdapter` Protocol，
  即圖中兩條「InboundMessage」邊所承載的契約本體。
- **`config.Settings`** — 環境變數與路徑推導（pydantic-settings），啟動時 fail-fast
  驗證；幾乎每個元件都讀它。

---

## Level 3（補充）— Component：Hermes 房間容器

這個 repo 也負責房間容器內容的 seed（MCP / plugin 模板）與 image 烘烤
（`Dockerfile.hermes`），所以補一張容器內部的元件圖。注意：gateway 本體是上游
`NousResearch/hermes-agent`，不是本 repo 的程式碼；本 repo 提供的是 MCP / plugin 模板。

```mermaid
flowchart TB
  classDef comp fill:#438dd5,color:#fff,stroke:#2e6295
  classDef ext fill:#999999,color:#fff,stroke:#6b6b6b
  classDef container fill:#1168bd,color:#fff,stroke:#0b4884

  router["Alice Office Router"]:::container
  llm["LLM Provider<br/><i>OpenAI 相容端點</i>"]:::ext
  google["Google<br/><i>Gmail / Drive API</i>"]:::ext
  roomdata[("data/&lt;room_key&gt;/ → /opt/data<br/><i>HERMES_HOME</i>")]:::container

  subgraph hermes["hermes_&lt;room_key&gt;（Docker 容器）"]
    gateway["<b>hermes-agent gateway</b><br/><i>上游框架，port 8642</i><br/>session 管理、skills 的 manifest-based sync、<br/>每次開機補齊 HERMES_HOME；<br/>依 config.yaml 載入 MCP 與 plugins"]:::comp
    secretary["<b>secretary MCP</b><br/><i>Node ESM（共用依賴烤在 /opt/node_modules）</i><br/>todo / attendance / expense / meeting /<br/>reminder / translate / maps / line"]:::comp
    gmail["<b>gmail MCP</b><br/><i>Python</i><br/>以房間 OAuth token 呼叫 Gmail API"]:::comp
    drive["<b>drive MCP</b><br/><i>Python（與 gmail 為逐 byte 複本結構）</i><br/>呼叫 Drive API"]:::comp
    plugins["<b>local-tools plugin</b><br/><i>Python scripts，跑在 /opt/tools/.venv（TOOLS_PYTHON）</i><br/>台灣法規、薪資引擎、OCR、工程計算、<br/>長期記憶、研究、瀏覽器自動化"]:::comp
  end

  router -- "POST /v1/chat/completions" --> gateway
  gateway -- "推理" --> llm
  gateway -- "MCP tool call" --> secretary
  gateway -- "MCP tool call" --> gmail
  gateway -- "MCP tool call" --> drive
  gateway -- "plugin command → script argv" --> plugins
  gmail -- "Gmail API" --> google
  drive -- "Drive API" --> google
  gateway <-- "sessions / skills / db 讀寫（bind mount）" --> roomdata
```

---

## 與其他文件的關係

- channel adapter 契約與分層的完整設計：`docs/channel-interface-design.md`
- Router ↔ Hermes 的 HTTP 協定細節：`docs/router-hermes-agent-protocol.md`
- 訊息端到端流程：`docs/line-hermes-message-flow.md`
- 環境變數與路徑：`docs/env-data-paths.md`、`docs/runtime-env-summary.md`
