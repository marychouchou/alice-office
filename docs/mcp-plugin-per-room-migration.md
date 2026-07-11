# MCP / Plugin：從「全房間共用掛載」改成「每房間各自 seed」

## 背景與動機

原本 `secretary-mcp/`（MCP）與 `plugins/local-tools/`（plugin）都是唯讀、全房間共用的
Docker bind mount——改一次程式碼，所有房間立刻一起變。使用者需求是**每個房間要能各自
客製化自己的 MCP／plugin**，不能互相影響。

決策（詳見 `/Users/mary/.claude/plans/effervescent-wondering-clarke.md`）：
- MCP、plugin 都改成 per-room **seed**（複製），不是 mount（掛載）。
- **write-once / frozen**：seed 只在房間第一次建立時發生一次，之後永不覆蓋。改
  repo 樣板只影響**之後新建立**的房間，既有房間要嘛自己改自己的副本，要嘛整個重建。
- 樣板整併到 `src/hermes/{mcp,plugin}/`。

## 核心機制

`/opt/data`（= Hermes 官方的 `HERMES_HOME`）本來就是每房間各自的 rw bind mount。
既然如此，per-room 可編輯**不需要新的 Docker 掛載**——只要在 `containers.run()` 之前，
把樣板原始碼「複製」進 `data/<room_id>/{mcp,plugins}/` 即可，房間之後隨便編輯，
`docker restart` 生效。

- `container_manager.py::_seed_templates()`：泛型 write-once 複製 helper，比照
  `_ensure_config_yaml()` 的「已存在就跳過」規則。
- `_ensure_mcp_seed()` / `_ensure_plugin_seed()`：分別把 `HERMES_TEMPLATES_DIR/mcp/*`
  複製到 `data/<room_id>/mcp/*`，`HERMES_TEMPLATES_DIR/plugin/*` 複製到
  `data/<room_id>/plugins/*`。MCP 額外把 `.env.example` seed 成該房間自己的 `.env`。
- `_format_mcp_section()`：不再是寫死單一 MCP 的字串模板，改成讀取每個已 seed 的
  MCP 目錄底下的 `mcp.manifest.yaml`（command/args/env/tools.exclude），動態產生任意
  數量的 `mcp_servers.<name>` 區塊，`args` 改寫成 `/opt/data/mcp/<name>/...`。
- `_build_volume_config()`：精簡到只剩 `data/<room_id>/ → /opt/data`（rw）一個掛載。

**ESM 依賴的特殊處理**：`server.mjs` 是 ESM（`"type": "module"`），**ESM 不吃
`NODE_PATH`**，只能靠從檔案位置往上找 `node_modules`。房間各自的 MCP 副本落在
`/opt/data/mcp/<name>/`（被 per-room 掛載蓋住），所以共用依賴改烤在再上一層的
`/opt/node_modules`（`Dockerfile.hermes`，用 `src/hermes/mcp/package.json` 描述聯集）
——所有房間、所有 MCP 共用同一份，往上找一定找得到。

## 目錄搬移

- `secretary-mcp/` → `src/hermes/mcp/secretary/`（新增 `mcp.manifest.yaml`）
- `plugins/local-tools/` → `src/hermes/plugin/local-tools/`
- 新增 `src/hermes/mcp/package.json`（共用 Node 依賴聯集，烤進 `/opt/node_modules`）

## 改動的檔案

| 檔案 | 改動重點 |
|---|---|
| `src/alice_office_router/config.py` | 移除 `HOST_PLUGINS_DIR`／`HOST_SECRETARY_MCP_DIR`；新增 `HERMES_TEMPLATES_DIR`（router 自己讀樣板用的路徑，**不是** `HOST_*` 那種給 Docker daemon 的路徑） |
| `src/alice_office_router/container_manager.py` | 新增 `_seed_templates` / `_ensure_mcp_seed` / `_ensure_plugin_seed` / `_load_mcp_manifest`；`_format_mcp_section` 改成讀 manifest 動態產生；`_build_volume_config` 精簡成單一掛載 |
| `Dockerfile.hermes` | MCP 共用依賴改烤進 `/opt/node_modules`（取代原本的 `/opt/secretary-mcp/node_modules`） |
| `Dockerfile`（router 自己的） | `COPY src/ ./src/` 改成 `COPY src/alice_office_router/ ./src/alice_office_router/`，避免把 `src/hermes/` 樣板無謂烤進 router image |
| `docker-compose.yml` | 新增 `./src/hermes:/app/hermes-templates:ro` 掛載＋`HERMES_TEMPLATES_DIR` 環境變數；移除舊的 `HOST_PLUGINS_DIR`／`HOST_SECRETARY_MCP_DIR` |
| `pyproject.toml` | 新增 `pyyaml` 依賴（manifest 需要真的 parse YAML）；ruff/mypy 排除路徑從 `plugins` 改成 `src/hermes` |
| `.env.example` / `.env` | 同步移除舊變數、新增 `HERMES_TEMPLATES_DIR` |
| `.dockerignore` | 新增 `**/node_modules/`，避免開發者本機的 `node_modules` 誤入任何一份 build context |
| `scripts/watch_restart.py` | 從監看 repo 層級的 `plugins/`／`secretary-mcp/`，改成監看**指定房間自己** seed 出來的 `data/<room_id>/{mcp,plugins}/` |
| `tests/test_container_manager.py` | 移除過時的掛載測試；新增 seed（write-once、複製內容、`.env` 產生）與 manifest 驅動 config.yaml 產生的測試 |
| `README.md` / `docs/env-data-paths.md` | 大幅改寫「C. Plugin / MCP」章節、環境變數表、專案結構樹；`env-data-paths.md` 新增「為什麼 MCP/plugin 路徑從 `HOST_*` 改成 `DATA_DIR` 那一類」的架構說明 |
| `CLAUDE.md` | 新增「Hermes Container Model」段落：`/opt/data` = `HERMES_HOME`、Hermes 自己開機會補完整目錄（`sessions/`、`skills/` 等）、這個 repo 只做 write-once 初始化 |

## End-to-end 驗證（2026-07-10，實機跑過）

用 `docker build -f Dockerfile.hermes` 建出的衍生 image + `docker compose up --build`
起 router，透過 `scripts/test_webhook.py` 對真的 Hermes container 跑過，確認：

1. **Image build**：`/opt/node_modules/@modelcontextprotocol/sdk`、`/opt/node_modules/zod`
   都在，smoke test 通過。
2. **Seed 真的發生**：router log 出現
   `Seeded template [secretary] into /app/data/<room>/mcp/secretary`、
   `Seeded template [local-tools] into /app/data/<room>/plugins/local-tools`。
3. **config.yaml 產生正確**：`mcp_servers.secretary.args` 指到
   `/opt/data/mcp/secretary/server.mjs`，`SECRETARY_LINE_USER_ID` 正確代換成 room_id，
   `tools.exclude`／`toolsets: [mcp-secretary]`／`plugins.enabled: [local-tools]` 都對。
4. **MCP 真的 spawn 成功**：`mcp-stderr.log` 出現 `[secretary-mcp] ready; lineUserId=...`
   ——證明 ESM 從 `/opt/node_modules` 解析依賴這條路真的通。
5. **Plugin／MCP tool 真的被 LLM 呼叫且執行成功**：
   `agent.log` 出現 `tool math completed`（plugin）與
   `tool mcp_secretary_todo_add completed`（MCP）。
6. **房間隔離是真的**：直接改房間 A 自己 seed 出來的
   `data/<room_A>/mcp/secretary/server.mjs`（加一段自訂字串），`docker restart` 後
   `mcp-stderr.log` 反映出改動；同時 room B 完全沒被建立過的獨立副本裡完全沒有這段改動。
7. **write-once / frozen 是真的**：repo 樣板 `src/hermes/mcp/secretary/server.mjs`
   全程沒被污染；事後新建立的 room C 拿到的是乾淨樣板，不是 room A 改過的版本。

測試用房間（`U_E2E_TEST`、`_B`、`_C`）與其 container 事後已清除。

## 追加改動：config.yaml 樣板也外部化成檔案

同一輪討論延伸出的後續要求：`config.yaml` 的預設樣板原本是寫死在
`container_manager.py` 裡的 Python 字串常數（`_CONFIG_YAML_TEMPLATE`），跟
`mcp/`、`plugin/` 樣板「看得到、放在 `src/hermes/` 底下」的風格不一致。改成：

- 新增 `src/hermes/config.yaml.template`——內容跟原本的 Python 常數逐字相同。
- `_ensure_config_yaml()` 改成讀 `HERMES_TEMPLATES_DIR/config.yaml.template`，
  用 `str.format()` 填入 `{model}`／`{base_url}`／`{plugins_section}`／`{mcp_section}`。
- 跟 `mcp/`／`plugin/` 不同：這**不是**逐檔複製（`_seed_templates`），因為
  `config.yaml` 需要執行期才知道的值（room_id、目前有哪些 MCP 被 seed），沒辦法
  單純複製貼上。
- 新增錯誤路徑：樣板檔案不存在時記錄 `logger.error` 並跳過該房間的 config.yaml
  產生（不拋例外中斷整個 container 建立流程），並補了對應測試
  `test_ensure_config_yaml_skips_when_template_missing`。

**實測驗證**（`docker compose up -d --build` 重建 router、建新房間 `U_E2E_TEST2`）：
router log 出現 `Wrote default config.yaml for room [U_E2E_TEST2]`，房間的
`config.yaml` 內容與改動前完全一致，container 健康並回答了訊息（200 OK）。測試房間
事後已清除。

單元測試：88/88 通過（含新增的 1 個測試），`ruff check` / `mypy --strict` 全綠。

## 已知的後續事項（未在這次改動範圍內）

- `.env` 目前 `HERMES_IMAGE=nousresearch/hermes-agent`（原生 image，沒有
  `/opt/node_modules` 也沒有 sympy/pymupdf）。要讓 secretary-mcp／math／OCR 真的可用，
  要 build 並切換到 `Dockerfile.hermes` 衍生版（見 README「Production 建法」）。
- 本機 Docker 裡多留了一份測試用的 `alice-hermes-agent:e2etest` image，可以
  `docker rmi alice-hermes-agent:e2etest` 清掉，或直接拿來當正式 tag 用。
- 多個 MCP 共用同一份 `/opt/node_modules` 依賴——如果未來某個新 MCP 需要跟現有
  MCP 衝突的套件版本，這個假設會被打破，目前沒有處理這種情況。
- `secretary-mcp` 自己的其他文件（`cron-recipes.md` 等）沒有逐字重新校對，只修了
  明顯過時／會誤導人的段落（README.md 的密鑰說明、`/opt/node_modules` troubleshooting）。
