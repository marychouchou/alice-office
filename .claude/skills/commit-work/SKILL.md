---
name: commit-work
description: 建立高品質、易審查的 git commit——依邏輯拆分、逐一 review 再 commit，訊息遵循 Conventional Commits。使用者要求 commit、拆分變更、或寫 commit message 時用。
---

# Commit Work：建立可審查的 git commit

本專案的 commit 規則已經定義在 `AGENTS.md`「Git」一節（Conventional
Commits：`feat:`／`fix:`／`chore:`／`refactor:`，實務上也用 `docs:`／`test:`
等其他標準 type；禁止自動 commit，只在明確要求時才執行；禁止提交 `.env`
或任何含 token 的檔案）。這個 skill 把「怎麼分批、怎麼審查再送出」的流程
具體化，不是另一套規則。

## 什麼時候用

使用者明確要求 commit、要求把混雜的變更拆成多個 commit、或要你寫 commit
message 時。**沒有要求就不要主動觸發**——這本身也是 `AGENTS.md` 的硬性規定。

## 流程

1. **檢視改動**：`git status`、`git diff`（unstaged）；改動多時先看
   `git diff --stat` 抓整體範圍。
2. **決定要不要拆成多個 commit**：依「改動理由」分——功能 vs 重構、
   格式化 vs 邏輯、測試 vs production code、依賴升級 vs 行為改變。同一個
   檔案混了兩種理由時，用 patch staging 分開。
3. **只 stage 這次要送出的部分**：優先用檔名（`git add <path>`），混雜變更
   時用 `git add -p` 逐個 hunk 挑；要取消 staging 用
   `git restore --staged -p` 或 `git restore --staged <path>`。**不要用
   `git add -A`／`git add .`**。
4. **review 真正要送出的內容**：`git diff --cached`，順手檢查有沒有
   secret／debug log／跟這次改動無關的格式化改動混進來。
5. **用一兩句話講清楚「改了什麼、為什麼」**：講不清楚代表這個 commit 範圍
   太大或混了兩件事，回到步驟 2 重新拆。
6. **寫 commit message**：Conventional Commits 格式，`type(scope): 摘要`，
   空一行後寫 what／why（不是實作細節的流水帳），需要時加
   `BREAKING CHANGE:` footer。多行訊息用 `git commit -v` 方便編輯。
7. **跑最小範圍的驗證**：至少跑一次相關測試，或
   `uv run ruff check . && uv run mypy src/ && uv run pytest`（commit 前
   AGENTS.md 規定的完整檢查）。
8. **重複 1–7 直到工作樹乾淨為止**。

## Commit message 範本

```
<type>(<scope>): <摘要（祈使句，具體）>

<改了什麼。>
<為什麼改。>
```

- 摘要用祈使句、具體（「新增」「修正」「移除」「重構」），不要空泛。
- 內文講行為與意圖，不要寫實作細節流水帳。
- Breaking change：header 加 `!` 或加 `BREAKING CHANGE:` footer。

## Reference

原始版本：https://github.com/softaworks/agent-toolkit/tree/main/skills/commit-work
