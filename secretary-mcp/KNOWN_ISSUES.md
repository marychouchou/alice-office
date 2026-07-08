# Known Issues

## #1 — LINE 接收音檔無法觸發 meeting_transcribe(adapter 限制,非 secretary 的 bug)

- **發現日期**:2026-06-30
- **狀態**:未修(使用者決定先記錄,暫不動碼)
- **影響**:在 LINE 傳語音/音檔想測 `meeting_transcribe` → 一定失敗。`meeting_summarize`(貼文字逐字稿)不受影響,正常可用。
- **根源位置**:`~/.hermes/hermes-agent/plugins/platforms/line/adapter.py`(hermes 核心碼,**不是** secretary-mcp)

### 現象 / log 證據

使用者於 2026-06-30 07:05 在 LINE 傳一個音檔,收到失敗訊息。`~/.hermes/logs/agent.log`:

```
07:05:23,455 WARNING line_platform.adapter: LINE: failed to cache audio payload:
             Refusing to cache non-image data as .m4a (starts with: '')
07:05:23,464 inbound message: msg='[audio]'
07:05:25,279 tool mcp_secretary_meeting_transcribe completed (0.00s, 120 chars)   ← 工具選對了
07:05:26,939 Turn ended: reason=text_response, api_calls=2
07:05:27,000 Skipping bare file path in reply (no file on disk):
             /home/.../​.hermes/audio_cache/audio_9d57a32d3d3c.mp3
```

agent 行為正常:正確選了 `meeting_transcribe`,拿到「找不到音檔」就放棄回報,**這一輪沒有多餘迴圈**(2 API calls、1 tool call)。

> 註:同一輪之後 log 出現的 `bg-review` 執行緒繞圈(skill_manage 重試報錯)是 hermes **每輪後自動跑的記憶/技能整理背景 agent**,跟本 issue 與 secretary 無關。

### 根因(兩層)

`_download_media()`(adapter.py:1055):

```python
data = await self._client.fetch_content(message_id)   # 這次回傳空 bytes (0 bytes)
ext = {"image":".jpg","audio":".m4a","video":".mp4","file":".bin"}.get(msg_type, ".bin")
return cache_image_from_bytes(data, ext=ext)           # 音檔也丟給「只收圖片」的函式
```

1. **直接原因**:`fetch_content` 抓回**空 bytes**(log 的 `starts with: ''`)。`fetch_content`(adapter.py:505)只判斷 `status >= 400`,LINE 對較大媒體可能先回 202「內容未就緒」→ 被當成功但 body 為空。
2. **結構性原因(更關鍵)**:就算抓到正確 m4a bytes 也會失敗。`cache_image_from_bytes`(`gateway/platforms/base.py:684`)會跑 `_looks_like_image()` 魔術位元檢查,**只接受 PNG/JPEG/GIF/BMP/WEBP,其餘一律 raise**。→ 等於這個 adapter build **只支援接收圖片**,audio/video/file 全被擋。
   - 佐證:agent.log:4744 另有一筆 PDF 上傳 `non-image data as .bin` 失敗,同一個 bug。

### 修法(待之後處理)

1. **拆掉只收圖片的閘門**:新增通用 `cache_media_from_bytes()`,或讓 `_download_media` 對 audio/video/file 走「不做 image magic 檢查」的存檔路徑。
2. **處理空下載 / 202**:`fetch_content` 對空 body 或 202 要重試或報錯,不要把 0 bytes 當成功。

兩處都在 `adapter.py`,屬 hermes-agent 核心碼,改完需 `hermes gateway restart`。

### 目前 workaround

- 會議記錄整理:在 LINE 直接**貼文字逐字稿**,走 `meeting_summarize`,正常可用。
- 語音轉錄:在 adapter 修好前無法經 LINE 使用。
