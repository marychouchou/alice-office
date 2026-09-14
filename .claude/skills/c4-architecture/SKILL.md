---
name: c4-architecture
description: 產生或更新本專案的 C4 架構圖（docs/architecture-c4.md）。當使用者要求畫架構圖、C4 diagram、context/container/component 圖，或架構有變動（新 channel、新 MCP/plugin、新外部整合、容器生命週期改變）需要同步圖面時使用。
---

# C4 架構圖：產生與維護

產出檔案固定是 `docs/architecture-c4.md`。更新前先讀現有檔案，只改受影響的圖與
段落；不要整份重寫掉「與官方定義的對照」等說明章節。

## 工作流程

1. **確認程式碼現況**：讀 `src/alice_office_router/` 的模組結構（channels/、
   core.py、container_manager.py、hermes_client.py、google_oauth.py、config.py）
   與 `src/hermes/{mcp,plugin}/` 的模板清單，確認圖面要反映的邊界與關係。
2. **改圖**：依下方的官方硬規則與本專案對應表修改 Mermaid 區塊。
3. **驗證（必做）**：跑 `.claude/skills/c4-architecture/validate.sh
   docs/architecture-c4.md`，四個區塊都要 OK；再用 Read 工具看渲染出的 PNG，
   檢查標籤有沒有重疊、標題有沒有被截斷、線有沒有交錯到不可讀。
4. 文件內的日期戳（「依 YYYY-MM-DD 的程式碼現況」）更新為當天。

## 官方定義的硬規則（依 c4model.com，2026-07 核對）

每一層有明確的「可以放什麼」，違反就不是 C4：

- **Level 1 System Context**：只有 Person、Software System 和互動意圖。
  **禁止**協定、路徑、技術名詞、環境變數（官方原文：focus on people and
  software systems rather than technologies, protocols and other low-level
  details）。受眾包含非技術人。
- **Level 2 Container**：container =「an application or a data store」，官方範例
  包含 file system——所以 `data/<room_key>/` 是合法的 container（data store）。
  這一層**應該**標技術選型與 container 之間的通訊協定。部署細節（clustering、
  load balancer、replication）不放這層。
- **Level 3 Component**：範圍是**單一 container**；component 必須與 container
  在**同一個 process space** 執行（a grouping of related functionality
  encapsulated behind a well-defined interface）。獨立行程不是 component，
  是另一個 container。官方提醒：component 圖只在有價值時才畫。
- **Notation 檢查清單**：每個元素明示型別（`[Person]`／`[Software System]`／
  `[Container: 技術]`／`[Component: 技術]`）＋一句職責描述；每條線**單向**、
  有標籤；container 之間的線要標協定；每張圖有標題；文件開頭有圖例。

## 本專案的 C4 對應（容易畫錯的地方）

- 系統邊界「Alice Office」= router **加上**所有房間的 Hermes 容器；LINE
  Platform、Google、LLM Provider、Docker Engine 都是外部 Software System。
  Docker Engine 不是部署細節——router 在執行期呼叫它動態建房間容器，是真實的
  執行期依賴。
- 一個房間的 Docker 容器裡跑多個行程（gateway、MCP servers、plugin 子行程），
  照 C4 定義它們**各自是 container**，Docker 容器只是部署邊界。Level 2 把整個
  房間容器畫成一個 container 是刻意簡化（對外只有 gateway 一個入口），行程級
  拆解畫在「Level 2 放大」那張圖——**不要**把 MCP/plugin 標成 Component。
- Router 的 Component 圖範圍是單一 FastAPI process，元件 = Python 模組。
  `channels.base`（InboundMessage 契約）與 `config.Settings` 是橫切元件，
  刻意不入圖、用文字說明——維持這個做法，畫進去會變蜘蛛網。

## Mermaid 語法規範

- **用 flowchart 語法，不用 Mermaid 原生 `C4Context`/`C4Container` 語法**——
  後者的排版引擎在超過六七個元素時標籤會大面積重疊（2026-07-15 實測）。
- 每張圖用 frontmatter 標題（`---\ntitle: "..."\n---`）。**frontmatter 標題裡
  不能有角括號**（`<room_key>` 會被當 HTML 吃掉，只剩 `hermes_`）；節點標籤內
  的角括號一律寫 `&lt;` `&gt;`。
- 共用 classDef 配色（即文件開頭圖例的定義；`system` 只給 Context 圖的
  Software System 本身用，顏色與 container 相同）：
  ```
  classDef person fill:#08427b,color:#fff,stroke:#052e56
  classDef system fill:#1168bd,color:#fff,stroke:#0b4884
  classDef container fill:#1168bd,color:#fff,stroke:#0b4884
  classDef comp fill:#438dd5,color:#fff,stroke:#2e6295
  classDef ext fill:#999999,color:#fff,stroke:#6b6b6b
  ```
- data store 用圓柱節點 `id[("...")]`；範圍邊界用 `subgraph`；節點標籤格式：
  `<b>名稱</b><br/>[型別: 技術]<br/><i>一句職責描述</i>`。
- 邊一律單向 `--  "標籤" -->`；讀寫類關係也用一條單向線標「讀寫」，不用 `<-->`。

## 維護時機

新 channel adapter、新 MCP/plugin 模板、新外部整合（如 Microsoft OAuth）、
container 生命週期或 seed 流程改變、`core.process_inbound` 的分層契約改變時，
對應的圖要同步更新。
