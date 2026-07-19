---
paths:
  - "src/hermes/mcp/**"
---

# Known Anti-Patterns：src/hermes/mcp（2026-07-12 掃描）

改到這些檔案時適用；每條寫明觸發時機和該做的事。

1. **`gmail/token_manager.py` 和 `drive/token_manager.py` 是逐 byte 相同的複本**
   （117 行）。這是 per-room seeding 以 template 目錄為單位的結構性結果，不是意外
   ——但修其中一個的 bug 必須同一個 commit 同步改另一個。若出現第三個需要
   token_manager 的 Python MCP：不要複製第三份，改烤進 image 的共用路徑（比照
   `/opt/node_modules` 處理 Node 依賴的方式）。
2. **secretary MCP 的 JSON store 樣板已重複 3 次**：`todo.mjs`／`attendance.mjs`／
   `expense.mjs` 各有一份 `readStore`/`writeStore`/`get*`/`save*`；`ok()` helper 在
   8 個 `tools/*.mjs` 各一份。已達 Rule of Three：下一個需要 per-user JSON store 的
   secretary tool 出現時，先抽 `tools/_store.mjs`（連同 `ok()`），不要複製第 4 份。
3. **`gmail/server.py`（8 個）和 `drive/server.py`（10 個）的 `call_tool` 是連續
   `if name ==` 鏈**，已超過 dispatch table 門檻（對同一個值 4 個以上分支）。
   下次在任一檔新增 tool 時，先改成 `{name: handler}` dict 再加新 tool。
4. **超過 3 層巢狀：兩份 `token_manager.py` 的 `get_access_token`**（if→for→try→if，
   2026-07-12 AST 實測 4 層；逐 byte 複本見第 1 條）。下次改到時用 early return／
   抽子函式打平到 3 層以內，兩份同一個 commit 同步改。
