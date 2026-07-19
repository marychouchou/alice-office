---
paths:
  - "src/hermes/plugin/**"
---

# Known Anti-Patterns：src/hermes/plugin（2026-07-12 掃描）

改到這些檔案時適用；每條寫明觸發時機和該做的事。

1. **`local-tools/tools.py` 有 4 個 handler（law／longmem／research／webdriver）在做
   同一種 `command` → argv 的 if/elif 翻譯**。第 5 個多 command handler 出現時，
   抽成表驅動的 `{command: argv_builder}` 共用寫法。
2. **超過 3 層巢狀：`local-tools/scripts/law/alice-tw-law-local.py` 的
   `find_law_records` 與 `import_json`**（2026-07-12 AST 實測，均 4 層）。下次改到時
   用 early return／抽子函式打平到 3 層以內；不必為打平專門開 PR。`.mjs` 檔掃描後
   無超標案例。
