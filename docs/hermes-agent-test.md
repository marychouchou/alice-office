# Hermes Agent Container 測試指南

測試對象：`hermes-agent:local`（FastAPI，Port 8642）  
不需要真實 LINE 帳號，即可完整驗證 LLM 連線、技能寫入、持久化等核心功能。

## 前置作業

```bash
# 建立 Docker 內網（只需跑一次）
docker network create hermes_global_net

# 建立測試用資料目錄
mkdir -p /Users/mary/dalue/alice-office-router/data/room_TEST/skills

# 清除可能殘留的同名容器
docker stop hermes_room_TEST 2>/dev/null; docker rm hermes_room_TEST 2>/dev/null

# 啟動測試容器
docker run -d \
  --name hermes_room_TEST \
  --network hermes_global_net \
  -p 18642:8642 \
  -v /Users/mary/dalue/alice-office-router/data/room_TEST:/root/.hermes \
  -e LINE_CHANNEL_ACCESS_TOKEN=test_token \
  -e OPENAI_API_KEY=sk-FDuf-guSKB6lHPhCBPSXGA \
  -e OPENAI_BASE_URL=https://spark2-vllm.dalue.co/v1 \
  -e LLM_MODEL=qwen3-next \
  hermes-agent:local

sleep 3
```

---

## Test 1 — Health Check

```bash
curl -s http://localhost:18642/health
```

預期回應：

```json
{"status": "ok", "skills_count": 0}
```

> `skills_count` 反映 `/root/.hermes/skills/` 內的 `.md` 檔案數量。

---

## Test 2 — LLM 基本對話

```bash
curl -s -X POST http://localhost:18642/line/webhook \
  -H "Content-Type: application/json" \
  -d '{"events":[{"type":"message","source":{"type":"user","userId":"user_001"},"message":{"type":"text","text":"用一句話介紹你自己"}}]}'

sleep 6
docker logs hermes_room_TEST 2>&1 | tail -20
```

預期：
- curl 立即回傳 `{"status":"ok"}`（非同步處理）
- logs 中看到 `POST /line/webhook 200`
- LLM 無連線 / 認證錯誤
- LINE push 會出現 `401 Unauthorized`（token 是假的，屬預期行為）

---

## Test 3 — 技能建立與磁碟寫入

```bash
curl -s -X POST http://localhost:18642/line/webhook \
  -H "Content-Type: application/json" \
  -d '{"events":[{"type":"message","source":{"type":"user","userId":"user_001"},"message":{"type":"text","text":"請建立一個叫做 weather_report 的技能，功能是查詢天氣並回報"}}]}'

sleep 8

# 確認技能檔案寫入 volume
ls -la /Users/mary/dalue/alice-office-router/data/room_TEST/skills/
cat /Users/mary/dalue/alice-office-router/data/room_TEST/skills/*.md
```

預期：
- `skills/` 下出現 `weather_report_md.md`
- 內容為 LLM 生成的 Markdown（包含說明、使用時機等）

---

## Test 4 — 重啟後技能持久化

```bash
docker restart hermes_room_TEST
sleep 4

curl -s http://localhost:18642/health
```

預期：`skills_count` > 0，代表 Volume 掛載正確、技能在重啟後仍存在。

---

## Test 5 — 技能載入進 LLM Context

```bash
curl -s -X POST http://localhost:18642/line/webhook \
  -H "Content-Type: application/json" \
  -d '{"events":[{"type":"message","source":{"type":"user","userId":"user_001"},"message":{"type":"text","text":"你現在有哪些技能？"}}]}'

sleep 8
docker logs hermes_room_TEST 2>&1 | tail -20
```

預期：
- LLM 被呼叫（logs 有新的 POST 200 記錄）
- 沒有新技能檔案被建立（只是問問題，不應觸發寫入）

---

## Test 6 — Group Source Type

```bash
curl -s -X POST http://localhost:18642/line/webhook \
  -H "Content-Type: application/json" \
  -d '{"events":[{"type":"message","source":{"type":"group","groupId":"group_001"},"message":{"type":"text","text":"hello"}}]}'
```

預期：`{"status":"ok"}`，`groupId` 正確被識別為 push target。

---

## Test 7 — 非文字事件（應靜默略過）

```bash
curl -s -X POST http://localhost:18642/line/webhook \
  -H "Content-Type: application/json" \
  -d '{"events":[{"type":"follow","source":{"type":"user","userId":"user_001"}}]}'
```

預期：`{"status":"ok"}`，不呼叫 LLM，不崩潰。

---

## Test 8 — 空 Events（LINE Webhook 驗證 Ping）

```bash
curl -s -X POST http://localhost:18642/line/webhook \
  -H "Content-Type: application/json" \
  -d '{"events":[]}'
```

預期：`{"status":"ok"}`。

---

## 查看完整 Logs

```bash
docker logs hermes_room_TEST 2>&1
```

---

## 清理

```bash
docker stop hermes_room_TEST && docker rm hermes_room_TEST
```

---

## 結果摘要

| 測試 | 驗證項目 |
|------|----------|
| 1 | Health endpoint 回應正常 |
| 2 | LLM 連線與非同步回覆流程 |
| 3 | 技能建立、`<create_skill>` 解析、磁碟寫入 |
| 4 | Volume 掛載 + 容器重啟後技能持久化 |
| 5 | 技能在下一次對話被讀進 system prompt |
| 6 | Group 聊天室事件正確路由 |
| 7 | 非文字事件不觸發 LLM |
| 8 | 空事件（LINE 驗證 ping）正確處理 |

## 已知行為

- LINE push 全程回傳 `401 Unauthorized`：使用假 token 的預期現象，換成真實 `LINE_CHANNEL_ACCESS_TOKEN` 即可解決。
- LLM 可能在普通對話中主動建立技能（e.g. 自我介紹時觸發 `hello_world` 技能）：這是 system prompt 設計的副作用，非 bug，但如需限制可修改 `main.py` 中的 prompt。
