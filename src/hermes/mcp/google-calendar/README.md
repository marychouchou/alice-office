# google-calendar（thin registration，無本地原始碼）

跟 `secretary/`、`gmail/`、`drive/` 不同：這個目錄底下**沒有 server 原始碼**。

Google Calendar MCP server 是官方發布的 npm 套件
[`@cocal/google-calendar-mcp`](https://www.npmjs.com/package/@cocal/google-calendar-mcp)
（**精確 pin 在 `2.6.2`**），跟 `secretary/` 的 `@modelcontextprotocol/sdk`、`zod` 一樣
烤進 image 的共用相依 `/opt/node_modules`（見根目錄 `Dockerfile.hermes`），
其 CLI 執行檔 `google-calendar-mcp` 也隨之出現在 `/opt/node_modules/.bin`
（已在 image 的 `PATH` 上）。

這個目錄本身只有兩個檔案：

- `package.json`——只用來讓 `tests/test_hermes_shared_node_deps.py` 檢查
  `@cocal/google-calendar-mcp` 的版本號有同步進 `src/hermes/mcp/package.json`
  （image build 實際安裝套件的地方），並不會被 `npm ci` 直接讀取。
- `mcp.manifest.yaml`——跟其他 MCP 一樣，被 `_ensure_mcp_seed` seed 進每個房間的
  `data/<room_id>/mcp/google-calendar/`，並被 `_format_mcp_section` 讀取寫進該房間的
  `config.yaml`。`command:` 直接指向 image 內建的 `google-calendar-mcp` 執行檔
  （不帶參數執行即啟動 stdio MCP server），不是 `node <path-to-seeded-script>`——
  因為真正的程式邏輯根本不在這份 seed 裡，seed 出來的東西只是「登記」。

## 環境變數

| 變數 | 說明 |
|------|------|
| `GOOGLE_OAUTH_CREDENTIALS` | Desktop/Installed 類型 GCP OAuth client JSON 路徑（`/opt/google-workspace/gcp-oauth.keys.installed.json`），**不是** `{"web": ...}` 格式——`@cocal/google-calendar-mcp` 只吃 `{"installed": ...}`。 |
| `GOOGLE_ACCOUNT_MODE` | tokens.json 裡的帳號 key，見下方「lowercase 規則」。 |
| `GOOGLE_CALENDAR_MCP_TOKEN_PATH` | tokens.json 路徑，**必須**明確設為 `/opt/google-workspace/tokens.json`（這個房間自己的掛載），不能沿用套件預設路徑，理由見下方。 |

### 為什麼所有路徑都必須明確指定（不能吃套件預設）

實機（container 內）驗證過的兩個事實：

1. Hermes gateway 與它 spawn 的每個 MCP subprocess 都以使用者 `hermes`
   （uid 10000，home=/opt/data）執行，**不是 root**。`/root` 是 mode 700，
   任何 `/root/...` 底下的路徑對 MCP process 都是 EACCES 打不開。
2. gateway 會設 `XDG_CONFIG_HOME=/opt/data/.config`，所以套件的「預設」
   token 路徑（`~/.config/...`）會被悄悄重導到**每個房間各自的** data dir，
   跟這裡明確指定的路徑對不起來——token 會存錯地方。

因此掛載點選在中立的 `/opt/google-workspace`（router 的
`container_manager.CONTAINER_GOOGLE_DIR`，每個房間各自掛的是 host 上
`data/<room_id>/google/`，不是共用目錄），所有 consumer（本 MCP、`gmail/`、
`drive/`）一律吃 manifest `env:` 明確給的絕對路徑。

## lowercase 規則（重要）

`@cocal/google-calendar-mcp` 驗證 `GOOGLE_ACCOUNT_MODE` 必須符合
`/^[a-z0-9_-]{1,64}$/`（只准小寫），但 LINE 的 room id 開頭是大寫
`U`/`C`/`R`。因此整個 Google 整合都用 **`room_id.lower()`** 當帳號 key
（見 `alice_office_router.google_oauth.account_key`）——`{account_key}` 這個
manifest 佔位符、tokens.json 的 key、OAuth start/callback 流程、gate 檢查，
全部一致使用這個 lowercase key，不能有任何一處漏掉轉換。

## requires_google_oauth

跟 `gmail/`、`drive/` 一樣，這份 manifest 有 `requires_google_oauth: true`——
只有當這個部署設定了 `GOOGLE_OAUTH_PUBLIC_URL` 且部署層的種子來源
`data/_google/gcp-oauth.keys.json` 存在時（`Settings.google_oauth_enabled`），
新建立的房間才會 seed 這個 MCP。因為是 write-once，先前在停用狀態下建立的房間
即使之後補齊設定也不會回頭補 seed，需要整個重建該房間。
