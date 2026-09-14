---
name: qa-test-planner
description: 幫沒有前端 UI 的 LINE OA router 產生手動測試計畫、測試案例、bug report——補足 pytest 自動化測試涵蓋不到的端對端場景（group chat 分派、session 輪替、container 生命週期）。
trigger: explicit
---

# QA Test Planner（本專案裁剪版）

上游 https://github.com/softaworks/agent-toolkit/tree/main/skills/qa-test-planner
原版是給有前端 UI／Figma 設計稿的產品用的（Figma 比對、e-commerce checkout
流程、行動裝置斷點）。本專案是 channel-free 的 FastAPI router，沒有前端、
沒有 Figma——這份是砍掉不適用內容後的版本，只保留手動測試規劃這件事本身。

**只在明確呼叫時觸發**（`/qa-test-planner` 或明確說要用這個 skill），不要
自動跳出來。

## 這個 skill 補的是什麼缺口

`AGENTS.md`「Testing」一節已經定義自動化測試慣例（Arrange-Act-Assert、mock
外部依賴、`uv run pytest`）；`docs/testing-paths.md` 是不開手機 LINE 時的
end-to-end 測試路徑。這個 skill 補的是**手動測試規劃與紀錄**——當某個場景
（例如新的 group chat gate 邏輯、session epoch 輪替）需要寫下明確的測試
步驟、或需要記錄一個手動發現的 bug 時用，不是取代 pytest。

## 什麼時候用

- 「幫我寫一份 [功能] 的測試計畫」
- 「幫我列 [場景] 的手動測試案例」
- 「幫我把這個 bug 寫成 bug report」
- 要規劃 regression 檢查範圍（例如改了 `container_manager.py` 後，哪些
  場景要重新手動過一次）

## 測試計畫範本

```markdown
# 測試計畫：[功能名稱]

## 範圍
- 涵蓋：[哪些 channel／流程]
- 不涵蓋：[明確排除的部分]

## 測試環境
- [本機 uv run fastapi dev ／ staging container]
- 測試房間：[room_id]，對應的 LINE OA 測試帳號

## 測試案例清單
[見下方案例範本，逐一列出]

## 風險與已知限制
| 風險 | 影響 | 因應 |
|---|---|---|

## 完成標準
- [ ] 所有 P0 案例通過
- [ ] 沒有未解決的 Critical bug
```

## 測試案例範本

```markdown
## TC-[編號]：[情境]

**優先級**：P0（關鍵路徑）｜P1｜P2
**類型**：webhook 解析｜gate 判斷｜container 生命週期｜session 行為

### 前置條件
- [房間狀態／container 是否已建立]
- [需要的測試資料]

### 步驟
1. [動作] → **預期**：[結果]
2. [動作] → **預期**：[結果]

### 實際結果
[執行後填]
```

## Bug report 範本

```markdown
# BUG-[編號]：[具體標題，含受影響的模組]

**嚴重度**：Critical｜High｜Medium｜Low
**環境**：[房間 ID／container 版本／commit hash]

## 重現步驟
1. ...

## 預期行為
...

## 實際行為
...

## 相關 log／證據
[docs/troubleshooting.md 提到的 container log 位置]
```

## 邊界（不要做的事）

- 不畫 Figma／UI 視覺比對——本專案沒有前端
- 不用 e-commerce／checkout 這類跟本專案無關的範例情境套模板
- 效能指標（req/s、CPU／記憶體）沒有既有 baseline 前不要編數字充版面，
  沒量過就寫「待建立 baseline」

## Reference

原始版本：https://github.com/softaworks/agent-toolkit/tree/main/skills/qa-test-planner
