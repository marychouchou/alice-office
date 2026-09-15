---
name: runtime-env
description: "這個容器的執行環境：官方 skill 直接照它自己的文件用 python／pip；Alice 自家工具用 tools-python；使用者傳來的檔案在 /opt/data/incoming/。要跑 Python、讀 PDF、或找使用者傳的檔案時先看這份。"
version: 2.0.0
author: alice
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Environment, Python, PDF, Files]
    related_skills: [ocr-and-documents]
---

# 執行環境（Alice 部署）

## 三個 Python，各管各的

| 指令 | 給誰用 | 說明 |
|---|---|---|
| `python`／`pip` | **Hermes 官方 bundled skill** | 獨立 venv `/opt/skills/.venv`。skill 文件寫什麼就照做：`python scripts/x.py`、`pip install x` 都能跑。已預裝 pymupdf、pymupdf4llm；其他套件用 `pip install` 現裝即可（只活在這個容器，容器重建會消失）。 |
| `tools-python` | **Alice 自家 plugin／工具** | 獨立 venv `/opt/tools/.venv`（sympy、selenium、pymupdf…）。官方 skill 不要用它；自家 plugin 的 script 也不要改用 `python`。 |
| `/opt/hermes/.venv/bin/python3` | Hermes 本體 | 不要動、不要往裡面裝東西。 |

出問題時先看是哪一欄：官方 skill 壞 → 看 `/opt/skills/.venv`；自家工具壞 → 看 `/opt/tools/.venv`。

## 路徑

- Skill 目錄在 `/opt/data/skills/<分類>/<名稱>/`。terminal 的工作目錄不是 skill 目錄，所以 skill 文件裡的 `scripts/x.py` 要寫成絕對路徑，例如
  `python /opt/data/skills/productivity/ocr-and-documents/scripts/extract_pymupdf.py`。
- 使用者從 LINE 傳來的圖片／PDF／音訊／檔案在 **`/opt/data/incoming/<原檔名>`**，訊息裡會給路徑（「已存放於 /opt/data/incoming/…」）。直接讀那個路徑，不要要求使用者再貼一次內容。

## 讀 PDF

**第一選擇：`image_ocr` 工具**（`local_tools` toolset）。把 `/opt/data/incoming/<檔名>.pdf` 的路徑當 `path` 傳進去即可：多頁一次讀完（預設 30 頁），有文字層的頁直接抽文字、只有掃描頁才走視覺模型，結果有 SHA-256 快取，而且**工具結果會留在對話紀錄裡**，之後幾輪都還查得到。

不要憑印象回答使用者傳來的檔案內容——不確定內容還在不在 context 裡就再呼叫一次 `image_ocr`（有快取，幾乎是瞬間完成）。

替代做法：官方 `ocr-and-documents` skill，照它的文件：

```bash
python /opt/data/skills/productivity/ocr-and-documents/scripts/extract_pymupdf.py "/opt/data/incoming/檔名.pdf"
python /opt/data/skills/productivity/ocr-and-documents/scripts/extract_pymupdf.py "/opt/data/incoming/檔名.pdf" --pages 0-2
python /opt/data/skills/productivity/ocr-and-documents/scripts/extract_pymupdf.py "/opt/data/incoming/檔名.pdf" --markdown
```

有文字層的 PDF 這樣就抽完了，不需要 OCR。抽出來是空的（掃描檔）才把頁面轉成 PNG，再用 `vision_analyze` 看圖：

```bash
python -c "
import pymupdf
doc = pymupdf.open('/opt/data/incoming/檔名.pdf')
for i, page in enumerate(doc):
    page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).save(f'/opt/data/incoming/page-{i+1}.png')
print(len(doc), 'pages')
"
```

## 圖片

主模型本身看得懂圖片，直接用 `vision_analyze` 給 `/opt/data/incoming/<檔名>` 的路徑即可。
