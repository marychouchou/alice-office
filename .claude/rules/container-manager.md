---
paths:
  - "src/alice_office_router/container_manager.py"
  - "src/alice_office_router/google_oauth.py"
---

# Known Anti-Patterns：container_manager / google_oauth（2026-07-12 掃描）

改到這兩個檔案時適用；每條寫明觸發時機和該做的事。

1. **`container_manager.py`（622 行）混了三種改動理由**：docker 生命週期、write-once
   seed（`_seed_templates`／`_ensure_*_seed`／`ensure_google_seed`）、config.yaml
   渲染（`_format_*`／`_load_mcp_manifest`／`_ensure_config_yaml`）。要新增 seed 種類
   或 config 渲染邏輯前先拆檔（seed → `room_seed.py`，渲染 → `hermes_config.py`），
   container 生命週期留在原檔；只是修 bug 則不必拆。
2. **特殊情況散落：`config.google_oauth_enabled` 的 if 出現在 5 處**——
   `container_manager.py` 的 `_ensure_mcp_seed`／`ensure_google_seed`／
   `_build_volume_config`，加上 `google_oauth.py` 的 `oauth_start`／
   `check_google_authorization`。「這個部署沒啟用 Google」這一個特殊情況，房間
   初始化流程的每一站都得各自記得檢查，漏一站就是 bug。現況可用；第二個需要
   OAuth gate 的整合（如 Microsoft）出現時，不要複製第二組散落的 `xxx_enabled`
   if——把房間初始化改成一張步驟清單（seed 步驟、mount 步驟、gate 檢查登記
   進去），讓「未啟用」＝不在清單上，而不是每站一個 if。
3. **特殊情況旗標：`get_or_create_container` 的 `needs_wait`**。
   running／stopped／missing 三條路徑用一個布林旗標記住「剛剛走了哪條」，只為了
   決定要不要等健康檢查。`_wait_until_ready` 對健康的 container 第一次 poll 就
   返回——永遠呼叫它即可消掉旗標和分支（代價是每則訊息多一次容器內 HTTP GET）。
   下次改這個函式時順手消掉。
4. **超過 3 層巢狀：`_wait_until_ready`**（with→while→try→if，2026-07-12 AST 實測
   4 層）。下次改到時用 early return／抽子函式打平到 3 層以內；不必為打平專門開 PR。
