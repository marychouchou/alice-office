# secretary-mcp（秘書 MCP 伺服器）

一個以 Node 撰寫的 MCP（Model Context Protocol）伺服器，透過 stdio 對 hermes agent
提供一整組「秘書」工具：待辦、會議、翻譯、出勤、費用、地圖，以及 LINE 訊息/檔案推送。

- **無原生模組相依**（純 JS），只需要 **Node ≥ 18**。
- 單一使用者設計：由環境變數 `SECRETARY_LINE_USER_ID` 指定這台伺服器所服務的使用者。
- 所有狀態都存在使用者家目錄的 JSON 檔（`~/.hermes/secretary-*.json`），沒有資料庫相依。

---

## 資料夾結構

```
secretary-mcp/
├── server.mjs             # 進入點：載入所有工具、連上 stdio transport
├── package.json           # 相依套件（@modelcontextprotocol/sdk、zod）
├── package-lock.json
├── config-snippet.yaml    # 要合併進 ~/.hermes/config.yaml 的片段
├── cron-recipes.md        # 提醒/定時摘要的 cron 範例（非 MCP 工具）
├── KNOWN_ISSUES.md        # 已知問題（例：LINE 音檔轉錄受 adapter 限制）
├── tools/                 # 每個檔案註冊一組工具
│   ├── todo.mjs           # 待辦事項
│   ├── meeting.mjs        # 會議轉錄/摘要（無狀態，指示型）
│   ├── translate.mjs      # 翻譯（無狀態，指示型）
│   ├── attendance.mjs     # 出勤打卡與工時報表
│   ├── expense.mjs        # 費用記帳與報表
│   ├── maps.mjs           # Google Places（新版）地點搜尋/詳情
│   ├── line.mjs           # LINE 文字/媒體/檔案推送
│   └── reminder.mjs       # 一次性提醒（底層走 hermes cron）
└── node_modules/          # npm install 產生
```

> **注意：`file-host` 是獨立服務，放在本專案的 sibling 目錄 `~/file-host/`。**
> 它有自己的行程、連接埠與對外網址，和 OAuth 伺服器完全無關。
> `line_send_media` / `line_send_file` 會把要傳送的檔案複製一份到「共用快取目錄」，
> 再由 file-host 以 HTTPS 對外提供下載。詳見 [`../file-host/README.md`](../file-host/README.md)。

---

## 工具說明

| 工具 | 說明 | 狀態存放 |
|------|------|----------|
| `todo_add` / `todo_list` / `todo_complete` / `todo_remove` | 個人待辦清單 | `~/.hermes/secretary-todos.json` |
| `meeting_transcribe` / `meeting_summarize` | 音檔中繼資料 + 會議記錄指示（實際內容由 agent 產生） | 無狀態 |
| `translate` | 翻譯指示（實際內容由 agent 產生） | 無狀態 |
| `attendance_in` / `attendance_out` / `attendance_status` / `attendance_report` | 每日上下班打卡與當月工時報表 | `~/.hermes/secretary-attendance.json` |
| `expense_add` / `expense_list` / `expense_report` / `expense_reimburse` / `expense_remove` | 費用記帳（含分類統計、報銷狀態） | `~/.hermes/secretary-expenses.json` |
| `maps_search` | 透過 Google Places API（新版）Text Search 搜尋地點 | 無狀態 |
| `maps_details` | 依 Place ID 查地點詳情；只有使用者要求評論時才設 `includeReviews=true`（計費層級較高） | 無狀態 |
| `line_send_message` | 傳純文字訊息（或連結）到 LINE | 無狀態 |
| `line_send_media` | 把圖片/影片/音檔以「內嵌媒體」傳到 LINE（透過 file-host） | 無狀態 |
| `line_send_file` | 把文件（PDF/Word…）以「可下載連結」傳到 LINE（透過 file-host） | 無狀態 |
| `reminder_set` / `reminder_list` / `reminder_cancel` | 一次性提醒；到點時透過 LINE 通知使用者 | 底層走 hermes cron |

### 幾個要點

- **LINE 的媒體/檔案傳送模型：** LINE 的 push API 沒有通用的「file」訊息型別，所以文件
  無法內嵌傳送 —— `line_send_file` 改傳一個連結。可內嵌的只有 `image`（.jpg/.jpeg/.png）、
  `video`（.mp4，需附預覽圖）、`audio`（.m4a，需帶長度毫秒）。媒體與文件都由
  [`file-host`](../file-host/README.md) 服務對外提供，並以 `SECRETARY_FILE_HOST_BASE_URL` 指到它的公開網址。
- **提醒的兩條路徑：** 簡單的一次性提醒直接用 `reminder_set`（它在內部呼叫
  `hermes cron create` 建立一個「到點呼叫 `line_send_message` 後自我刪除」的工作）。
  比較複雜、需要每天/每週跑的自動化，則用 hermes 內建的 `cronjob` 工具搭配
  `line_send_message`，範例見 [cron-recipes.md](cron-recipes.md)。這些定時工作**不是** MCP 工具。
- **時間解析（reminder）：** 支援相對時間（`10分鐘後`、`in 5 minutes`、`2小時後`）與
  絕對時鐘時間（`14:30`、`明天9:00`、`2026-07-01 14:30`）。時鐘時間一律以 **Asia/Taipei（UTC+8）**
  解讀，不依賴 MCP 主機的本地時區。

### 地圖計費提醒

`maps_search` 與不帶評論的 `maps_details` 使用 Essentials + Pro 層級。
在 `maps_details` 設 `includeReviews=true` 會額外觸發 Enterprise + Atmosphere 層級（費用明顯較高）。
工具描述已指示 agent：除非使用者明確詢問評論，否則不要設這個旗標。

---

## 環境變數

| 變數 | 必填 | 說明 |
|------|------|------|
| `SECRETARY_LINE_USER_ID` | 是 | LINE user id（`U…`）—— 個人狀態的儲存鍵，也是 `line_*` 工具的預設收件對象 |
| `GOOGLE_MAPS_API_KEY` | 是 | 已啟用 Places API（新版）的 Google Maps 金鑰；未設定時地圖工具會回設定錯誤 |
| `SECRETARY_LINE_CHANNEL_ACCESS_TOKEN` | 是 | LINE Messaging API 的 channel token；`line_*` 傳送工具需要 |
| `SECRETARY_FILE_HOST_BASE_URL` | 是 | file-host 的公開網址（**不含** `/files` 後綴）；`line_send_media` / `line_send_file` 需要 |
| `FILE_CACHE_DIR` | 否 | 暫存檔案的共用快取目錄。**必須和 file-host 服務設成同一個路徑。** 未設定時預設為 `~/.cache/secretary-mcp/file-cache`（或 `$XDG_CACHE_HOME/secretary-mcp/file-cache`） |
| `HERMES_BIN` | 否 | `reminder_*` 用來呼叫的 hermes 執行檔路徑；未設定時依序找 `~/.local/bin/hermes` → PATH |

> 慣例：像 `GOOGLE_MAPS_API_KEY`、`LINE_CHANNEL_ACCESS_TOKEN` 這類機密，請設在系統環境
> （`~/.profile` / `~/.bashrc`），再於 `config.yaml` 用 `${VAR}` 參照，**不要**把明碼寫進 `config.yaml`。

---

## 首次安裝（在 VM 上）

```bash
# 1. 用 GCP console SSH（⚙ → Upload file）上傳 secretary-mcp 壓縮檔

# 2. 解壓到家目錄
mkdir -p ~/secretary-mcp && unzip -o secretary-mcp-batch2.zip -d ~/secretary-mcp

# 3. 安裝相依套件（純 JS，不會編譯原生模組）
cd ~/secretary-mcp && npm install

# 4. 煙霧測試 —— 應印出 ready 那行後停住等待（Ctrl+C 結束）
SECRETARY_LINE_USER_ID=你的LINE_USER_ID node server.mjs
# 預期 stderr： [secretary-mcp] ready; lineUserId=你的LINE_USER_ID

# 5. 把機密設進系統環境（不要寫進 config.yaml 的明碼）
echo 'export GOOGLE_MAPS_API_KEY="AIzaSy..."' >> ~/.profile
echo 'export LINE_CHANNEL_ACCESS_TOKEN="你的LINE_channel_token"' >> ~/.profile
source ~/.profile

# 6. 把 config-snippet.yaml 的內容「逐區段合併」進 ~/.hermes/config.yaml
#    （把 YOUR_USER / YOUR_LINE_USER_ID 換成實際值；不要整段覆蓋原檔）

# 7. 若要用 line_send_media / line_send_file，另外啟動 file-host 服務
#    （見 ../file-host/README.md），並把 SECRETARY_FILE_HOST_BASE_URL 指到它的公開網址

# 8. 重啟 hermes gateway
hermes gateway run --replace > /tmp/hermes_gateway.log 2>&1 &
sleep 5

# 9. 驗證
hermes mcp list   # secretary 應顯示為 ✓ enabled
```

`config-snippet.yaml` 要合併的區段：`toolsets` 加入 `mcp-secretary`；
`mcp_servers` 加入 `secretary`（`command: node`、`args` 指向 `server.mjs`）；
該 server 的 `env`（上表的環境變數）；`agent.tool_use_enforcement` 設為 `true`；
`agent.disabled_toolsets` 停用部分內建工具（web、browser、video、image_gen、video_gen、
x_search、moa、todo、context_engine、homeassistant、spotify、yuanbao）；
以及 `tools.tool_search.threshold_pct` 設為 `20`。

### 後續更新（沒有新相依時）

```bash
# 只改了 .mjs 檔的話不需要重跑 npm install
unzip -o secretary-mcp-batchN.zip -d ~/secretary-mcp
hermes gateway run --replace > /tmp/hermes_gateway.log 2>&1 &
```

---

## 測試指令

```bash
# 待辦 todo
hermes -z "新增一個高優先級任務：明天下午三點開團隊會議"
hermes -z "列出我的待辦"
hermes -z "把任務 <id> 標記為完成"

# 出勤 attendance
hermes -z "幫我上班打卡"
hermes -z "幫我下班打卡"
hermes -z "顯示我這個月的出勤報表"

# 費用 expense
hermes -z "記一筆 350 元的午餐費用"
hermes -z "顯示這個月的費用報表"
hermes -z "把費用 <id> 標記為已報銷"

# 翻譯 / 會議
hermes -z "翻成英文：今天天氣很好"
hermes -z "把這段逐字稿整理成會議記錄：..."

# 地圖（需要 GOOGLE_MAPS_API_KEY）
hermes -z "找台北 101 附近的拉麵店"
hermes -z "查一下地點 <placeId> 的詳細資訊"
hermes -z "大家對 <地點> 的評價如何？"   # agent 應以 includeReviews=true 呼叫 maps_details

# 提醒 reminder
hermes -z "10分鐘後提醒我去寄信"
hermes -z "提醒我明天9點開會"
hermes -z "列出我目前的提醒"

# LINE 傳送（需要 channel token 與 file-host）
hermes -z "傳一張圖片 /path/to/photo.jpg 到 LINE 給我"
hermes -z "把這份 PDF /path/to/doc.pdf 用連結傳到 LINE"
```

---

## 常見問題（QA）

| 症狀 | 檢查方向 |
|------|----------|
| `hermes mcp list` 沒有出現 secretary | 看 `/tmp/hermes_gateway.log` 是否有 spawn 錯誤；確認 `args` 路徑正確；確認 `node -v` ≥ 18 |
| 工具有被呼叫但狀態檔沒寫入 | 確認 `~/.hermes/` 可寫入；目錄與檔案會自動建立 |
| 模型都不呼叫這些工具 | 確認 `~/.hermes/config.yaml` 的 `toolsets` 有包含 `mcp-secretary` |
| 地圖回 `GOOGLE_MAPS_API_KEY is not configured` | 在 `~/.profile` 設好環境變數，並重啟 hermes gateway 行程 |
| 地圖回 API error 403 | 金鑰沒有啟用 Places API（新版）；到 GCP Console → APIs & Services 啟用 Places API (New) |
| 地圖回 API error 400 | 檢查 `X-Goog-FieldMask` 的欄位是否對；通常是混進了舊版 API 的欄位名 |
| `line_*` 回 `SECRETARY_LINE_CHANNEL_ACCESS_TOKEN is not configured` | 設定 LINE channel token 環境變數並重啟 gateway |
| `line_send_media/file` 回 `SECRETARY_FILE_HOST_BASE_URL is not configured` | 啟動 file-host 服務並設定其公開網址（不含 `/files` 後綴） |
| LINE 收到媒體連結但打不開（404/410） | 多半是 **file-host 與 line.mjs 的快取目錄不一致**：兩邊都要用同一個 `FILE_CACHE_DIR`；或連結已超過 TTL（預設 24 小時） |
| 在 LINE 傳語音想測 `meeting_transcribe` 一定失敗 | 這是 hermes LINE adapter 的限制（只收圖片），**不是** secretary 的 bug。詳見 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。暫時解法：直接貼文字逐字稿走 `meeting_summarize` |

---

## 移植到另一台機器的重點

1. **相依極少**：只要 Node ≥ 18 + `npm install`，沒有原生編譯、沒有資料庫。
2. **狀態可攜**：待辦/出勤/費用都存在 `~/.hermes/secretary-*.json`，複製這幾個檔即可搬資料。
3. **機密走環境變數**：`GOOGLE_MAPS_API_KEY`、`LINE_CHANNEL_ACCESS_TOKEN` 設在系統環境，
   `config.yaml` 只用 `${VAR}` 參照，搬機器時重設環境即可。
4. **file-host 要一起搬**：若用到 LINE 媒體/檔案，file-host 是獨立服務，需另外部署並公開一個
   HTTPS 網址；且 **file-host 與本專案 `line.mjs` 必須共用同一個 `FILE_CACHE_DIR`**。
5. **時區固定**：`reminder` 的時鐘時間寫死為 Asia/Taipei（UTC+8），不受 MCP 主機時區影響。
