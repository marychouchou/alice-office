# Google 授權改版計畫：延遲授權＋群組逐人授權（task10）

狀態：**設計定案，實作中**（2026-09-18 以 Fable 5.1 擬定；實作交 Opus）。
圖解版（使用者體驗流程、機制圖）：`docs/google-auth-per-member-design.html`。
實作完成後，本文件的「現況」段落應改寫成設計說明，或併入
`google-workspace-integration-summary.md`。

## 0. 一句話

不再用 Google 授權擋下使用者的第一則訊息；agent 真的碰到 Google 工具沒 token 時，
router 才在回覆裡放授權連結，使用者授權完 router 自動把剛才的問題重跑一次並推播答案。
群組裡 token 逐人存放，**每一回合一律以發話者的身分執行 Google 工具**，授權連結也只給
發話者。

## 1. 現況（實作前，供對照）

- Gate 只認房間：`core._take_turn` 每回合呼叫 `check_google_authorization(room_key)`，
  沒 token 就回 `blocked`，agent 完全不跑（`core.py:686-692`）。sender_id 沒進到
  gate、`/oauth/start`、`/oauth/callback` 任何一處。
- `tokens.json` 每房一份，內層 key 是 `account_key(room_id)`＝room id 小寫；三個 MCP
  的 `GOOGLE_ACCOUNT_MODE` 在 config.yaml 寫死成這個 key（write-once，既有房間改不了 env）。
- 群組後果：連結廣播給全群，誰點誰的 Google 帳號就成了群組的帳號，第二個人點會靜默覆蓋，
  之後所有成員都能透過 agent 操作那個帳號。
- `GOOGLE_OAUTH_GATE=false` 已經是「不擋」，但 MCP 的錯誤文字沒有連結，agent 給不出網址。

## 2. 已驗證的事實（決定做法的關鍵）

| 事實 | 出處 | 影響 |
|---|---|---|
| 三個 MCP 每次 tool call 都重新讀 token 檔；calendar MCP 只快取 `OAuth2Client` 物件、每次呼叫都 `setCredentials` 重設 | `@cocal/google-calendar-mcp` 2.6.2 bundle `executeWithHandler`→`ensureAuthenticated`→`loadAllAccounts`；`gmail/drive token_manager.load_all_tokens` | router 在回合之間換檔即可，**不用重啟容器或 MCP** |
| calendar MCP 用檔案裡「所有 key」當帳號集合，`GOOGLE_ACCOUNT_MODE` 對 tool dispatch 無效；gmail/drive 嚴格用 `GOOGLE_ACCOUNT_MODE` 當 key | 同上 | 換入的檔案內層 key 必須維持 `account_key(room_id)`，且只放一個 key |
| 換入的 entry **必須含 `access_token`**（即使已過期），否則 calendar 視為沒帳號 | bundle L611 | `_store_token` 已寫 access_token，維持 |
| MCP 刷新 token 後會 **整檔覆寫** token path（透過 symlink 寫到目標檔）；calendar 在 fallback 路徑遇到 JSON 壞掉會 **unlink** 檔案 | bundle L284, L419-427；`token_manager.save_all_tokens` | 每個成員檔用 temp+rename 原子寫；換檔只在 room turn lock 內、回合之間做 |
| calendar MCP 有 5 分鐘的「依名稱找行事曆」快取，快取 key 是帳號 key 集合 | bundle `CalendarRegistry` | 同一 key 換人，5 分鐘內用名稱找行事曆可能對到前一人的清單；`primary` 與含 `@` 的 id 不受影響。v1 接受，記進 troubleshooting |
| `/opt/google-workspace` 是整個目錄 rw bind mount | `container_manager._build_volume_config` | host 端在目錄內換 symlink，容器內立即可見；symlink 目標要用**相對路徑** |
| line-bot-sdk 已有 `TextMessageV2`／`MentionSubstitutionObject` | `uv run python -c "import linebot.v3.messaging"` | 群組真 @mention 可做，但列為加分項 |
| `InboundMessage` 是 pydantic model | `channels/base.py:44-77` | pending 訊息可直接序列化存檔、原樣重跑 `process_inbound` |
| `process_inbound` 對訊息無副作用，可重複呼叫 | `core.py:789-821` | 重跑走正常管線（lock、session hygiene、group prompt） |

## 3. 設計

### 3.1 身分規則（唯一一條）

```
member_key(msg) =
    account_key(msg.sender_id)   if msg.is_group and msg.sender_id
    account_key(room_key)        if not msg.is_group          # 1:1 房間：房間本身就是成員
    None                         if msg.is_group and not sender_id   # LINE 沒給身分
```

- 1:1 房間的成員就是房間，所以 1:1 和群組走**同一條**換檔路徑，只是 1:1 每次換到同一檔。
- `None`：群組裡沒加 OA 好友的成員（LINE 不給 userId）。Google 工具一律失敗，marker 換成
  固定提示「LINE 沒提供你的身分，請先加我為好友再試」，不給連結。
- 「要操作誰的資源」不由 agent 判斷；A 問 B 的行事曆就是用 A 的帳號去查，看不看得到由
  Google 的分享設定決定。

### 3.2 Token 存放

```
data/<room>/google/
  gcp-oauth.keys.json                  （不變）
  gcp-oauth.keys.installed.json        （不變）
  tokens.json  -> members/<member_key>.json    ← 相對 symlink，指向「當前發話者」
  members/
    <member_key>.json                  ← { "<account_key(room)>": {access_token, refresh_token, expiry_date, token_type, scope} }
```

- 成員檔內層 key 固定是 `account_key(room_id)`（MCP env 寫死的那個），不是成員 key。
- **換檔**：`select_member_tokens(room_id, member_key)`：建立 temp symlink → `os.replace` 蓋到
  `tokens.json`（原子）。目標不存在也照指：MCP 讀不到檔 → 回「No token」→ marker。
  已指向同一目標則 no-op（1:1 的常態）。
- **寫入**：callback 存 token 用 temp+rename 寫 `members/<member_key>.json`。
- **既有房間遷移**（一條規則，不分 1:1／群組）：`tokens.json` 是一般檔案時，搬成
  `members/<account_key(room)>.json`。1:1 房間的 member_key 正好等於它，授權無縫延續；
  群組房間這個 key 沒人會用到，等於群組舊 token 作廢、成員各自重新授權（這正是要修的問題）。
- 新增 `Settings.room_google_members_dir(room_id)`、`room_google_member_tokens_path(room_id, member_key)`、
  `room_pending_auth_path(room_id, member_key)`（AGENTS.md：同一子路徑不得在兩處拼字串）。

### 3.3 授權連結：marker 走 reply seam（比照 `outbox://`）

- 固定字串 marker：`google-auth://request`（不需要隨機 token，它不是能力憑證；替換結果只會是
  「這個房間、這回合發話者」的連結，別人觸發也只是拿到自己的連結）。
- 誰會產生 marker：
  1. gmail／drive `token_manager.get_access_token` 的兩段錯誤文字（no token／refresh 失敗）改成
     含 marker 與指示：「請在回覆裡把 `google-auth://request` 原樣單獨放一行」。兩份檔案同一
     commit 改、順手把 4 層巢狀打平（`.claude/rules/hermes-mcp.md` 第 1、4 條）。
  2. calendar MCP 是第三方，錯誤文字不可控 → 靠 system prompt 規則：「任何 Google 工具回報
     未授權／no token／no authenticated accounts，就在回覆裡單獨一行放 `google-auth://request`」。
     寫在 `group_context.DIRECT_SYSTEM_PROMPT`／`GROUP_SYSTEM_PROMPT`（既有房間立即生效）和
     `src/hermes/skill/alice/runtime-env/SKILL.md`（要 rebuild image），與 `outbox://` 的兩層
     做法一致（`docs/file-share-design.md` §9）。
- 新模組 `auth_links.py`（鏡射 `file_links.py`）：`publish_auth_links(text, msg, config) -> (text, requested: bool)`：
  沒 marker 零 I/O 直接回傳；有 marker → 依 3.1 算 member_key → 換成
  「{sender_name} 請點此連結 Google 帳號：{PUBLIC_BASE_URL}/oauth/start?user_id={room}&member={member_key}」
  （或 None 身分的固定提示）並把這則 `InboundMessage` 寫進 pending（3.4）。
- 在 `core._take_turn` 的 seam 與 `publish_file_links` 並列，同在 room lock 內。
  `RouteResult.gate_status` 新值 `"auth_link"` 讓 turn envelope 看得出這回合發了連結。
- 群組訊息開頭用 `sender_name` 純文字點名即可；改成 `TextMessageV2` 真 @mention 列為加分項
  （限制：被 @ 的人必須在該群組，一則最多 20 個）。

### 3.4 授權完自動接續（pending re-run）

- pending 存檔：`data/<room>/router_state/pending_auth/<member_key>.json`＝`InboundMessage` JSON＋ts。
  同一成員再觸發就覆寫（只保留最後一則）。TTL 與 `_pending` 一樣 10 分鐘，過期不重跑。
- `/oauth/start?user_id=<room>&member=<member_key>`：`_pending[state] = (room_id, member_key, ts)`。
- `/oauth/callback`：存進成員檔 → `asyncio.create_task(on_authorized(room_key, member_key))`
  → 回 HTML「授權成功，答案稍後會出現在 LINE」。`on_authorized` 是 main.py lifespan 註冊進
  `google_oauth` 的 hook（避免 google_oauth ↔ core 循環 import）。
- `core.resume_pending_auth(room_key, member_key)`：讀 pending、刪檔、找到該 channel 的 adapter、
  呼叫 `adapter.resume(msg)`。
- `ChannelAdapter` Protocol 新增 `async def resume(self, msg: InboundMessage) -> None`；
  `channels/__init__.py` 提供 `adapter_for(channel_name)` registry（main.py 建 adapter 時登記）。
  - LINE：把 `_process_and_reply` 的 `reply_token` 改成 optional，`resume` 就是同一函式、
    無 reply token、無 loading animation → 全部走 push，envelope 照常 `record_turn`。
    重跑的回覆前面加一行「{sender_name} 已完成 Google 授權」（群組才加）。
  - API channel：`resume` 只記 log 並丟棄（沒有人可以推播），文件寫明。
- 重跑就是再呼叫一次 `process_inbound(msg)`：會再走 reset 檢查、gate（現在只剩 notice）、
  session hygiene（閒置太久會 rotate，無妨）、群組會把授權期間累積的背景一起帶上（可接受，記錄）。

### 3.5 Gate 函式的去留

- `check_google_authorization` 改簽名為 `(room_id, member_key, config) -> ("ok"|"notice", msg)`，
  只剩「缺 Drive scope 提醒」；`blocked` 狀態刪除。
- `GOOGLE_OAUTH_GATE` 設定**刪除**（同 commit 改 `.env.example`、`docker-compose.yml`、README）。
  `Outcome` Literal 保留 `"blocked"` 供舊 envelope 讀取，程式不再產生；`logging-design.md` §5.7 註明。
- 暖機子系統（`_warm_container` 等）唯一觸發點是 blocked 回合，會失去呼叫者。**保留機制、
  換觸發點**：LINE `follow`（1:1 加好友，目前 adapter 直接忽略）與 `join`（群組，已處理）事件
  → `core.warm_room(room_key)`。體驗上比現在更早暖機。

### 3.6 換檔時機

`core._take_turn`（room lock 內）：

```
reset 檢查
member_key = member_key_for(msg)
select_member_tokens(room, member_key)          # 新增；None → 指向 members/_anonymous.json
status = check_google_authorization(room, member_key)   # 只剩 notice
turn = _reply_for(...)
text = publish_file_links(...); text, requested = publish_auth_links(...)
```

`select_member_tokens` 在 `google_oauth_enabled` 為 False 時 no-op——這是第 6 個
`google_oauth_enabled` if（`.claude/rules/container-manager.md` 第 2 條）。可接受，因為仍是同一個
整合；若之後再加第二個 OAuth 整合，屆時做 step-list 重構。

## 4. 模組配置

| 檔案 | 動作 |
|---|---|
| `google_tokens.py`（新） | 成員檔讀寫、`select_member_tokens`、legacy 遷移、`_check_token`（從 google_oauth 搬來）。google_oauth.py 現 413 行，加東西前先拆 |
| `google_oauth.py` | routes、`_pending`（三元組）、`check_google_authorization`（member 版、無 blocked）、`on_authorized` hook |
| `auth_links.py`（新） | marker regex、替換、pending 寫入；鏡射 `file_links.py` |
| `core.py` | `_take_turn` 換檔＋seam；`resume_pending_auth`；`warm_room`；刪 blocked 分支。建議先做一個純搬移 commit 把暖機子系統移到 `warmup.py`（core.py 已 821 行、混多種改動理由） |
| `channels/base.py`、`channels/__init__.py` | Protocol `resume`、adapter registry |
| `channels/line/adapter.py` | `_process_and_reply(reply_token: str \| None)`、`resume`、`follow` 事件 → warm |
| `channels/api.py` | `resume` no-op |
| `config.py` | 三個新路徑 method；刪 `GOOGLE_OAUTH_GATE` |
| `group_context.py` | 兩個 system prompt 加 marker 規則 |
| `src/hermes/mcp/gmail/token_manager.py`、`drive/token_manager.py` | 錯誤文字含 marker；打平巢狀；兩檔同 commit |
| `src/hermes/skill/alice/runtime-env/SKILL.md` | 加 marker 規則（需 rebuild image 才到既有房間） |
| `main.py` | 註冊 hook、adapter registry |
| `.env.example`、`docker-compose.yml`、README | 刪 `GOOGLE_OAUTH_GATE` |

MCP manifest **不用改**（token path、`GOOGLE_ACCOUNT_MODE` 都維持），所以既有房間的
write-once config.yaml 不受影響。

## 5. 實作順序（每步可獨立 commit、測試全綠）

0. **前置（已完成 2026-09-18）**：task9 已 commit 在 `feat/file-share-links`，本分支
   `feat/lazy-google-auth` 從它開出。
0b. **LINE API 可指向本機 stub**：`Settings.LINE_API_BASE_URL`（預設空＝官方 host）傳給
   line-bot-sdk 的 `Configuration(host=...)`；`scripts/line_stub.py` 起一個本機 HTTP server
   記錄 `/v2/bot/message/reply`、`/push` 的 JSON 到 stdout 與檔案，並對 profile／group member
   查詢回固定假資料。目的：後面每一步的 e2e 都能在本機看到 router 送出了什麼。同 commit 更新
   `.env.example`、`docker-compose.yml`、`docs/testing-paths.md`。
1. `google_tokens.py`＋Settings 路徑＋legacy 遷移＋symlink 換檔。1:1 行為不變（成員＝房間）。
   測試：成員檔格式、symlink 相對路徑、原子換檔、遷移、None 身分、disabled no-op。
2. OAuth routes member 化（start 多 `member` 參數、`_pending` 三元組、callback 寫成員檔）；
   gate 函式 member 化、刪 blocked、刪 `GOOGLE_OAUTH_GATE`；`_take_turn` 接上換檔。
   測試：`test_google_oauth.py` 的 start／callback／gate 全部改寫；`test_core.py` 的 9 個
   blocked 測試改成「沒 token 也進 agent」。
3. Marker：token_manager 兩檔、system prompts、SKILL.md、`auth_links.py`、seam、`gate_status="auth_link"`、
   pending 寫入。測試比照 `test_file_links.py` 的 substitute 系列。
4. Resume：Protocol、registry、LINE `resume`、API no-op、`on_authorized` hook、callback 觸發、
   success HTML 文案、群組「已完成授權」前綴。測試：stub adapter 收到 resume；TTL 過期不重跑；
   pending 被覆寫只留最後一則。
5. 暖機改觸發：`follow`／`join` → `warm_room`；`_warm_container` 測試改對應。
6. 文件：`prd.md` FR-06 改寫；`user-manual.md` 84-100（刪「再傳一次」）、140-146、161-163；
   `sequence-diagrams.md` §5、§7；`google-workspace-integration-summary.md` 83-112、212-247；
   `group-chat-design.md` §5、§8、§11；`file-share-design.md` §9 加交叉引用；
   `architecture-c4.md` 加 auth_links 元件；`logging-design.md` §5.7；`troubleshooting.md`
   加 calendar 5 分鐘快取、symlink 檢查指令；`docs/README 表` 更新本檔狀態。
7. 驗證與部署（§6）。

## 6. 驗證

- 單元：`uv run ruff check . && uv run mypy src/ && uv run pytest`。
- 本機 e2e（需真 LINE，API channel 沒有 push 走不完 resume）：
  1. 新 1:1 房：傳「今天幾號」→ 直接回答（不再被擋）。
  2. 傳「明天有什麼會」→ 回覆含授權連結；`ls -l data/<room>/google/` 看到 `tokens.json -> members/…`。
  3. 點連結授權 → 不再傳訊息，LINE 自動收到答案。
  4. 群組：A 點名問行事曆 → 連結點名 A；B 問 → 連結點名 B；A 授權後 B 問仍拿到自己的連結；
     A 問「B 明天有空嗎」→ 用 A 帳號查（看 Google 分享決定）。
  5. 既有 1:1 房間（有舊 `tokens.json`）：升級後第一則訊息照常能用 Google，目錄被遷移。
- 容器內：`docker exec hermes_<room> ls -l /opt/google-workspace/` 確認 symlink 解析正確。
- Oregon 部署：依 memory 的 upgrade 流程；`.env` 刪 `GOOGLE_OAUTH_GATE`；rebuild image
  才能把 SKILL.md 送到既有房間（system prompt 的規則不用 rebuild 就生效）。

## 7. 已知限制與後續

- **群組裡別人可以點 A 的連結**：Google callback 拿不到 LINE 身分。v1 靠「連結點名 A」＋
  「A 已完成授權」公告讓誤綁可見。後續選項：`.env` 開關改成把連結 push 到 A 的私訊
  （能拿到 sender_id 就代表 A 已加好友，一定推得到）；或前面加一段 LINE Login 驗身分。
- 公告目前只寫 LINE 顯示名稱，不含 Google email（需多要 `userinfo.email` scope）。
- 跨成員需求（「找大家共同空檔」）需要多個成員的 token 同時在場，本設計刻意不支援；
  將來可由 router 對每位成員各跑一回合再彙整。
- calendar MCP 依名稱找行事曆的 5 分鐘快取（§2）。
- API channel 的 pending 會被丟棄。
