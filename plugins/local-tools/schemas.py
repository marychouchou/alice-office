"""Tool schemas for the local-tools plugin (OpenAI function-calling format)."""

HR_SCHEMA = {
    "description": (
        "根據員工名冊 CSV + 出勤 CSV 計算台灣薪資，輸出 Excel (.xlsx)。"
        "使用 2026 台灣勞保/健保/勞退費率，不呼叫 LLM。"
        "輸出包含 Payroll、RiskFlags、Employees、Attendance、Parameters 五個工作表。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "employees_csv": {
                "type": "string",
                "description": "員工主檔 CSV 路徑（必要欄位：employee_id, name, base_salary, plan）",
            },
            "attendance_csv": {
                "type": "string",
                "description": "出勤 CSV 路徑（必要欄位：employee_id；選填：overtime_1_34_hours, unpaid_leave_days 等）",
            },
            "out_xlsx": {
                "type": "string",
                "description": "輸出 Excel 路徑（例如 /tmp/payroll-2026-06.xlsx）",
            },
            "config_path": {
                "type": "string",
                "description": "薪資設定 JSON 路徑（選填，預設使用內建 2026.tw 版本）",
            },
        },
        "required": ["employees_csv", "attendance_csv", "out_xlsx"],
    },
}

LAW_SCHEMA = {
    "description": (
        "查詢本機台灣法規資料庫（全國法規資料庫鏡像，不呼叫 LLM）。"
        "command=search 做全文 FTS 搜尋；command=stats 看資料庫統計；"
        "command=mirror 從 law.moj.gov.tw 下載/更新法規資料（需要網路）。"
        "支援縮寫：勞基法、個資法、消保法等。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["search", "stats", "mirror"],
                "description": "search=全文搜尋；stats=資料庫統計；mirror=下載更新",
            },
            "query": {
                "type": "string",
                "description": "搜尋關鍵字，command=search 必填（支援法規名稱、條號、縮寫）",
            },
            "limit": {
                "type": "integer",
                "description": "搜尋結果筆數上限，預設 8",
            },
            "force": {
                "type": "boolean",
                "description": "command=mirror 時強制重新下載，預設 false",
            },
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": ["ch_law", "ch_order", "en_law", "en_order"]},
                "description": "command=mirror 時指定資料來源，預設全部四種",
            },
        },
        "required": ["command"],
    },
}

MATH_SCHEMA = {
    "description": (
        "工程數學計算機，不呼叫 LLM，使用 SymPy 做精確符號運算。"
        "支援：微分/導數、不定/定積分、解方程式、極限、矩陣行列式、矩陣反矩陣、線性方程組、一般數學表達式求值。"
        "支援中文指令（如「微分 x^3 對 x」）與全形符號。"
        "回傳 exact（精確值）、decimal（數值）、latex（LaTeX 字串）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "數學表達式或指令，例如："
                    "'integrate sin(x) from 0 to pi'、"
                    "'微分 x^3 對 x'、"
                    "'solve x^2-4=0 for x'、"
                    "'det [[1,2],[3,4]]'"
                ),
            },
        },
        "required": ["expression"],
    },
}

LONGMEM_SCHEMA = {
    "description": (
        "本機長期記憶系統（SQLite + FTS5，不呼叫 LLM）。"
        "可儲存使用者偏好、購物連結、公司規則等，並以全文搜尋查詢。"
        "敏感資料（密碼、信用卡）會被自動拒絕。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["remember", "recall", "context", "record_turn", "delete"],
                "description": (
                    "remember=儲存記憶；recall=查詢記憶；"
                    "context=取得完整使用者情境摘要；"
                    "record_turn=記錄對話輪次（自動抽取記憶）；"
                    "delete=軟刪除記憶"
                ),
            },
            "user_id": {"type": "string", "description": "使用者識別碼（必填）"},
            "text": {
                "type": "string",
                "description": "記憶內容或對話文字（remember/record_turn 必填）",
            },
            "query": {
                "type": "string",
                "description": "搜尋關鍵字（recall/context 選填）",
            },
            "role": {
                "type": "string",
                "enum": ["user", "assistant", "system", "tool"],
                "description": "record_turn 時的發言角色（必填）",
            },
            "conversation_id": {
                "type": "string",
                "description": "record_turn 時的對話 ID（選填）",
            },
            "memory_type": {
                "type": "string",
                "description": "remember 時的記憶類型（preference/shopping_link/company_rule 等，預設 preference）",
            },
            "title": {
                "type": "string",
                "description": "remember 時的標題（選填，自動推導）",
            },
            "memory_id": {
                "type": "string",
                "description": "delete 時要刪除的記憶 ID（從 recall 結果取得）",
            },
            "limit": {
                "type": "integer",
                "description": "recall/context 結果筆數上限（預設 8）",
            },
        },
        "required": ["command", "user_id"],
    },
}

RESEARCH_SCHEMA = {
    "description": (
        "查詢本機 AI 助理生態系索引（中國及全球，離線，不呼叫 LLM）。"
        "可搜尋專案、列出全部或篩選類別、依能力需求取得推薦。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["search", "list", "recommend"],
                "description": (
                    "search=依關鍵字搜尋（名稱、別名、capabilities 功能均在搜尋範圍，例如搜 'memory' 可找出支援記憶功能的專案）；"
                    "list=列出全部專案，可用 category 按大分類篩選，但無法按 capabilities 功能篩選；"
                    "recommend=依需求描述推薦最合適的專案"
                ),
            },
            "query": {
                "type": "string",
                "description": "command=search 的搜尋關鍵字（必填）",
            },
            "need": {
                "type": "string",
                "description": (
                    "command=recommend 的需求描述，可用預設值或自由文字："
                    "memory/office/home/voice/workflow/channels/skills"
                ),
            },
            "category": {
                "type": "string",
                "description": "command=list 按類型篩選（選填）",
            },
            "limit": {
                "type": "integer",
                "description": "搜尋/推薦結果上限（預設 8）",
            },
        },
        "required": ["command"],
    },
}

OCR_SCHEMA = {
    "description": (
        "辨識圖片或 PDF 中的文字與內容（單頁）。"
        "呼叫 vision API（Qwen2.5-VL）辨識，支援繁體中文、印刷體、考卷、合約、一般文件與照片。"
        "PDF 自動取第一頁轉換後辨識；結果有 SHA-256 cache，相同檔案不重複呼叫 API。"
        "回傳辨識文字（text 欄位），由呼叫端決定如何整理。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "圖片或 PDF 的絕對路徑（支援 .jpg/.jpeg/.png/.webp/.pdf）",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "辨識提示（選填）。例如：「只抄錄題目與選項」、「描述圖片場景」、「找出金額與日期」。"
                    "留空則使用預設全文 OCR 提示。"
                ),
            },
        },
        "required": ["path"],
    },
}

WEBDRIVER_SCHEMA = {
    "description": (
        "瀏覽器自動化（Selenium + Firefox headless）。"
        "command=health 檢查環境；command=open 開啟網址並取得頁面文字；"
        "command=shopping 台灣電商搜尋/瀏覽商品頁；command=uber 叫車前置流程。"
        "注意：依賴尚未安裝（selenium + firefox-esr + geckodriver），目前無法使用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["health", "open", "shopping", "uber", "cleanup"],
                "description": "health=環境檢查；open=開啟網址；shopping=電商搜尋；uber=叫車；cleanup=清理舊截圖",
            },
            "url": {"type": "string", "description": "command=open/shopping 的目標網址"},
            "instruction": {
                "type": "string",
                "description": "command=shopping/uber 的自然語言指令（例如「蝦皮 衛生紙 售價」）",
            },
            "pickup": {"type": "string", "description": "command=uber 的出發地"},
            "dropoff": {"type": "string", "description": "command=uber 的目的地"},
            "days": {
                "type": "integer",
                "description": "command=cleanup 時刪除幾天前的截圖（預設 7，由 ALICE_BROWSER_SS_MAX_DAYS 控制）",
            },
        },
        "required": ["command"],
    },
}
