# Channel Interface 設計文件

日期：2026-07-14
狀態：已定案，實作計畫見 [channel-interface-plan.md](channel-interface-plan.md)

## 1. 動機

目前 LINE 是 repo 接收外界訊息的唯一管道，造成兩個問題：

1. **開發不便**：測任何東西都要經過 LINE console + 公網 webhook。
2. **擴充受阻**：預期的入口有 LINE、Telegram、自訂 TUI、之後的 mobile app，
   但 LINE wire 語意（驗簽、reply token、4500 字泡泡上限）直接寫在 `router.py` 裡。

## 2. 現況盤點（2026-07-14 掃描）

接縫其實已經存在——核心只認 `room_id: str` 進、`text: str` 出：

| 分類 | 檔案 | 狀態 |
|---|---|---|
| 完全 channel-agnostic | `hermes_client.py`、`container_manager.py`、`config.py`（扣掉 2 個 LINE 設定） | 不用動 |
| LINE wire format（~480 行） | `line_verify.py`、`line_events.py`、`line_format.py`、`line_client.py`、`line_dedup.py` | 已集中，搬家即可 |
| 混合（要拆的地方） | `router.py`（202 行）：泛用編排 + LINE 分派/回覆/token-fallback | 本設計的主要工作 |
| 邊緣耦合 | `google_oauth.py` 的訊息模板與成功頁寫死「請回到 LINE」 | 文案去 LINE 化 |

## 3. Prior art

調查了 hermes-agent gateway、Errbot、Rasa channels、Hubot adapters、matterbridge，
以及重量級對照組 Bot Framework Activity schema。共識形狀高度一致：

- **每個 channel 一個 webhook endpoint**（`/webhooks/<channel>`），驗簽與 reply-token
  等 wire 語意封在 adapter 內，永不外洩到 core。
- **inbound 正規化成一個小 model**（text + 身分），不建 Activity 式全型別 schema
  （那份規格書 1,700 行，是 N 通道 × N bot 的平台生意才回本的投資）。
- **對話身分用 channel 前綴的複合 key**（matterbridge 的 `protocol.instance`、
  hermes 的 `platform:chat_type:chat_id`）。
- **outbound 只強制純文字**；圖片、按鈕等能力方法可選、預設降級成文字（Rasa 的做法）。
- **刻意不抽象**：格式渲染（正準格式是 plain text／markdown，各 adapter 自己 render）、
  房間生命週期、threading。

特別的發現：**hermes-agent 內建的 gateway 就是一套 channel adapter 系統**
（`gateway/platforms/base.py` 的 `BasePlatformAdapter`，接 20+ 平台，但沒有 LINE）。
本 router 等於在容器外幫 LINE 補做 hermes gateway 的事（另見
[hermes-agent-line-gateway-comparison.md](hermes-agent-line-gateway-comparison.md)），
所以介面命名直接鏡射它。

來源：
- https://github.com/NousResearch/hermes-agent（`gateway/platforms/base.py`、`ADDING_A_PLATFORM.md`）
- https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-internals
- https://rasa.com/docs/rasa/connectors/custom-connectors
- https://errbot.readthedocs.io/en/latest/errbot.backends.base.html
- https://github.com/42wim/matterbridge/blob/master/bridge/bridge.go

## 4. 設計

### 4.1 核心概念：一個 channel-free 函式 + 兩類入口

```
第三方 webhook 通道（LINE、之後的 Telegram）
  POST /webhooks/line ──┐
                        ├──► core.process_inbound(msg) ──► list[str]（依序送回房間的純文字）
第一方 API 通道（TUI／mobile／dev curl）
  POST /webhooks/api ───┘
```

core 是**一個函式，不是框架**。它不呼叫任何 channel 的送訊 API——收訊息、跑 gate、
呼叫 agent，然後**回傳**要送回房間的文字清單，由呼叫它的 adapter 自己負責送出：

```python
# core.py
async def process_inbound(msg: InboundMessage, config: Settings) -> list[str]:
    """Google gate → get_or_create_container → ask_hermes_agent。

    回傳依序要送回房間的純文字訊息（gate notice、agent 回覆…）。
    gate blocked 時回傳只含授權引導訊息的清單，不呼叫 agent。
    """
```

為什麼是回傳值而不是 adapter.send()（Rasa/Errbot 的做法）：本系統的回覆嚴格
發生在處理該則 inbound 的請求範圍內，沒有非同步推送；回傳值讓 core 變成可直接
單元測試的純編排函式，也讓第一方 API 通道能同步把回覆放進 HTTP response。
LINE 的 reply token 因此**根本不需要進 core**——adapter 的 handler 解析事件時
留著 token，拿到回傳值後自己決定 reply-token-優先-push-兜底。
（若未來出現主動推播需求——cron 提醒、agent 主動發話——屆時才在 Protocol 加
`send_text()`，見 §6。）

### 4.2 介面

```python
# channels/base.py
class InboundMessage(BaseModel):
    """core 唯一認識的入站形狀。媒體在 adapter 內已下載落地、組好佔位文字。"""
    channel: str      # adapter 名，如 "line"
    room_key: str     # 全域唯一房間鍵，見 §4.3
    text: str

class ChannelAdapter(Protocol):
    name: str         # 唯一識別；同時是 room_key 前綴與 webhook 路徑段

    def api_router(self) -> APIRouter:
        """掛在 /webhooks/{name} 的 FastAPI router。驗簽、wire 解析、dedup、
        媒體下載、回覆送出全在裡面；出口只有 core.process_inbound。"""
```

註冊就是 `channels/__init__.py` 裡一個普通函式
`enabled_adapters(config) -> list[ChannelAdapter]`，`main.py` 逐一 mount。
不做動態發現、不做 plugin 系統。

### 4.3 room_key：channel 前綴的複合鍵

- 格式：`f"{channel}_{native_id}"`，例：`line_U1234...`、`tg_123456789`、`api_mary`。
- **分隔符必須是 `_` 不能是 `:`**——room_key 流進 docker 容器名
  （`hermes_<room_key>`，同時是 network hostname，總長需 < 63）與 Google
  `account_key`（regex `^[a-z0-9_-]{1,64}$`），兩者都不收 `:`。
- native_id 的字元集由各 adapter 保證：LINE 是 `[UCR][0-9a-f]{32}`；
  API 通道驗證 `[a-z0-9-]{1,32}`。
- core 全程只用 room_key；adapter 在邊界換算
  `native_id = room_key.removeprefix(f"{self.name}_")`。
- **既有 LINE 房間（裸 id）做一次性遷移**，不留「LINE 不加前綴」的永久特殊情況
  （CLAUDE.md 消除特殊情況原則）。遷移含資料夾改名、舊容器移除、seeded 檔內
  舊 id 字串改寫、tokens.json key 改寫，見 plan Phase 3。

### 4.4 第一方通道不偽裝成 webhook

TUI 和 mobile app 是我們控制兩端的 client：沒有第三方 wire format、驗簽、
reply token。給它們的是一個 trivial adapter（Rasa 的 `rest` channel 定位）：

```
POST /webhooks/api/messages
Authorization: Bearer $API_CHANNEL_TOKEN
{"room_key": "line_U1234...", "text": "..."}
  → 200 {"replies": ["...", ...]}
```

- 回覆**同步**放在 HTTP response，不推送；回的是 agent 原始輸出（markdown 不剝除，
  TUI／mobile 自己 render——正準格式歸 adapter 管的原則）。
- 允許對**任意** room_key 發訊（含 `line_*`）：token 持有者是操作者，這正是
  開發時「用 curl 打進任何房間」的除錯入口。TUI／mobile 正式使用時用自己的
  `api_<id>` 房間。
- `API_CHANNEL_TOKEN` 未設定 → 通道不掛載。
- mobile 的主動推播（notification）不在本設計範圍，屆時另議（websocket／polling）。

### 4.5 目錄結構（回答「太 flat 嗎」）

現況頂層 12 檔還在可接受邊緣，但 channel 化後會到 ~20 檔，該收了。
原則：**只收 channel 檔案群**；其他檔案已有 CLAUDE.md 的拆分訊號在管
（反模式 5：`container_manager.py` 等新增 seed 種類才拆；反模式 7：第二個
OAuth gate 出現才動），訊號沒到不為結構而結構。

```
src/alice_office_router/
├── __init__.py
├── main.py              # app factory；mount enabled_adapters() 的 routers
├── config.py            # Settings（含新的 API_CHANNEL_TOKEN）
├── core.py              # process_inbound：gate → 容器 → agent → list[str]
├── hermes_client.py     # router ↔ hermes HTTP 協定（不動）
├── google_oauth.py      # OAuth routes + gate（第二個 gate 出現才升 gates/）
├── container_manager.py # 容器生命週期（CLAUDE.md 反模式 5 的訊號到了才拆 rooms/）
└── channels/
    ├── __init__.py      # enabled_adapters(config)
    ├── base.py          # InboundMessage、ChannelAdapter Protocol
    ├── api.py           # 第一方通道（單檔即可，它很小）
    └── line/
        ├── __init__.py
        ├── adapter.py   # LineAdapter：webhook handler + 回覆送出（原 router.py 的 LINE 半邊）
        ├── client.py    # ← line_client.py
        ├── dedup.py     # ← line_dedup.py
        ├── events.py    # ← line_events.py
        ├── format.py    # ← line_format.py
        └── verify.py    # ← line_verify.py
```

tests/ 鏡射：LINE wire-format 測試搬進 `tests/channels/line/`，
`test_router.py` 拆成 `test_core.py`（channel-agnostic，fake adapter）＋
`tests/channels/line/test_adapter.py`（wire format）。

## 5. 刻意不抽象（anti-overdesign 防線）

- **不做統一 rich-message schema**。正準格式是純文字/markdown，各 adapter 自己
  render（LINE 剝 markdown、TUI 直接渲染、Telegram 可用 parse_mode）。
- **不抽象媒體外送、threading、房間生命週期**——用到再說。
- **dedup 是 per-adapter 的事**（LINE `webhookEventId`、Telegram `update_id`、
  API 通道不需要）；`dedup.py` 的邏輯可重用，但不上提成 core 概念。
- **不做 adapter 動態註冊／plugin 機制**。
- **介面不先磨光**：LINE 成為第一個 adapter + API 通道就停。Telegram 是介面
  第一次被第二個 webhook 通道真正驗證的時刻，屆時允許修 `base.py`
  （Rule of Three 精神）。

## 6. 預留的擴充點（訊號 → 動作）

| 訊號 | 動作 |
|---|---|
| 第二個 webhook 通道（Telegram） | 建 `channels/telegram/`；第一次真的碰到介面不合就修 `base.py` |
| 主動推播需求（cron 提醒、agent 主動發話、mobile notification） | Protocol 加 `async send_text(room_key, text)`，各 adapter 實作 |
| 第二個 OAuth gate（Microsoft） | 依 CLAUDE.md 反模式 7 改成步驟清單，`google_oauth.py` 升 `gates/` |
| adapter 間出現第 3 份重複邏輯 | 抽進 `channels/base.py` 或共用模組 |

## 7. 附帶修正（實作時一併處理）

1. **Session header 語意**：`hermes_client.py:59` 用 `X-Hermes-Session-Id: room_id`，
   但 hermes 官方語意中 session id 是 transcript-scoped（`/new`／`/reset` 會輪替），
   穩定對話身分應該用 `X-Hermes-Session-Key`。改用後對話會開新 session
   （舊 transcript 檔案仍在房間資料夾）。實作時需真機驗證 hermes `api_server`
   接受此 header。
2. **gate 文案去 LINE 化**：`google_oauth.py` 的 `_SUCCESS_HTML`「請回到 LINE」→
   「請回到聊天視窗」。
3. **殘留 bytecode**：`src/alice_office_router/channels/__pycache__/` 是舊分支產物
   （在 `__pycache__` 內、無對應 `.py`，PEP 3147 下不會被 import，無害但清掉）。
