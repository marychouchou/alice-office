---
name: agent-md-refactor
description: 當 AGENTS.md／CLAUDE.md 等 agent 指示檔案又變得臃腫、出現重複或矛盾規則時，用 progressive disclosure 原則重新拆分——精簡根檔案，細節搬進 docs/ 或 .claude/rules/。
---

# Agent MD 重構：維護臃腫的 agent 指示檔案

本專案目前的結構本身就是 progressive disclosure 的成果：`CLAUDE.md` 只有一行
`@AGENTS.md` 轉介；`AGENTS.md` 是唯一權威來源（見 commit
「docs: make AGENTS.md the canonical agent instructions」）；細節分散在
`docs/*.md`（見 `AGENTS.md` 的文件地圖表格）與 `.claude/rules/`（按檔案路徑
`paths:` frontmatter 觸發的 anti-pattern 登記，改到對應檔案才自動載入）。

這個 skill 的用途是**維護**這個結構，不是從零建立——當 `AGENTS.md` 又長回去、
或 `docs/*.md`／`.claude/rules/*.md` 之間出現矛盾規則時才用。

## 什麼時候用

- 使用者說「AGENTS.md 太長了」「幫我整理 agent 指示」「這兩條規則互相矛盾」
- 定期健檢：`AGENTS.md` 出現只在特定任務類型才相關的細節，不再是「跨任務都
  適用的通用規則」

## 五階段流程

1. **抓矛盾**：通讀 `AGENTS.md`、`docs/*.md`、`.claude/rules/*.md`，找出互相
   衝突的規則。逐一列出，問使用者要以哪條為準——不要自己武斷決定。
2. **抽出根檔案該留的東西**：只留「100% 任務都適用」的規則——專案一句話描述、
   非標準工具鏈指令（`uv run ...`）、Growth Discipline 這類跨檔案的結構性規則、
   DO NOT 這類硬性禁止事項。
3. **分類其餘規則放哪裡**：
   - 因「特定檔案路徑改動」才相關的 anti-pattern → `.claude/rules/`（新增時
     設好 `paths:` frontmatter，不用使用者主動去讀）
   - 因「特定任務類型」才需要的深度說明 → `docs/*.md`，並同步加進
     `AGENTS.md` 的文件地圖表格——這條 `AGENTS.md` 本身就有寫「新增
     `docs/*.md` 時要同步加進這張表，不然等於沒寫」，不要漏掉。
4. **精簡根檔案**：改完後檢查 `AGENTS.md` 是否還在合理長度；`CLAUDE.md`
   維持只有 `@AGENTS.md` 一行，不要把內容抄回 `CLAUDE.md`。
5. **標記可刪除的規則**：太籠統（「寫乾淨的程式碼」）、跟 ruff／mypy 已強制的
   規則重複、或已過時，列出來問使用者是否要刪——不要自己直接刪掉。

## 檢查清單

- [ ] 沒有遺留矛盾規則
- [ ] `AGENTS.md` 只剩通用規則，細節都搬到 `docs/*.md` 或 `.claude/rules/`
- [ ] 新增的 `docs/*.md` 都已加進 `AGENTS.md` 的文件地圖表格
- [ ] `.claude/rules/*.md` 的 `paths:` frontmatter 正確對應到會觸發的檔案
- [ ] 沒有規則憑空消失（除非是使用者同意刪除的）

## Reference

原始版本：https://github.com/softaworks/agent-toolkit/tree/main/skills/agent-md-refactor
