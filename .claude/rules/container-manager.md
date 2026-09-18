---
paths:
  - "src/alice_office_router/container_manager.py"
  - "src/alice_office_router/google_oauth.py"
  - "src/alice_office_router/room_seed.py"
---

# Known Anti-Patterns：container_manager / google_oauth / room_seed（2026-07-12 掃描，2026-09-15 更新）

改到這幾個檔案時適用；每條寫明觸發時機和該做的事。

1. **write-once seed 已於 2026-09-15 抽到 `room_seed.py`**（`_seed_templates`／
   `_google_gated_template_names`／`ensure_mcp_seed`／`ensure_plugin_seed`／
   `ensure_google_seed`／`ensure_soul_seed`，新增 SOUL.md persona seed 時順手抽的）。
   `container_manager.py` 仍混兩種改動理由：docker 生命週期、config.yaml 渲染
   （`_format_*`／`_load_mcp_manifest`／`_ensure_config_yaml`）。要新增 config
   渲染邏輯前先拆檔到 `hermes_config.py`——但這需要先把 `CONTAINER_*_DIR` 常數搬到
   `config.py`（`_format_mcp_section` 依賴它們，留在 `container_manager.py` 會讓
   `hermes_config.py` 跟 `container_manager.py` 互相 import），是獨立的一次 refactor；
   只是修 bug 則不必拆。
2. **特殊情況散落：`config.google_oauth_enabled` 的 if 出現在 6 處**——
   `room_seed.py` 的 `ensure_mcp_seed`／`ensure_google_seed`，
   `container_manager.py` 的 `_build_volume_config`，`google_tokens.py` 的
   `select_member_tokens`，加上 `google_oauth.py` 的
   `oauth_start`／`check_google_authorization`。「這個部署沒啟用 Google」這一個
   特殊情況，房間初始化流程的每一站都得各自記得檢查，漏一站就是 bug。現況可用；
   第二個需要 OAuth gate 的整合（如 Microsoft）出現時，不要複製第二組散落的
   `xxx_enabled` if——把房間初始化改成一張步驟清單（seed 步驟、mount 步驟、gate
   檢查登記進去），讓「未啟用」＝不在清單上，而不是每站一個 if。
3. ~~`get_or_create_container` 的 `needs_wait` 旗標／`_wait_until_ready` 4 層巢狀~~
   ——2026-09-15 已消掉：一律呼叫 `_wait_until_ready`（健康容器第一次 poll 就返回），
   health poll 抽成 `_is_healthy`。**不要把旗標加回來**：core 的 `warm_room` 暖機在
   room lock 外建容器，「已 running」不代表 api_server 已就緒。
