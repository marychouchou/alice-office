#!/usr/bin/env bash
# 抽出 markdown 檔內所有 ```mermaid 區塊，逐一用 mermaid-cli 渲染成 PNG。
# 驗證兩件事：語法可渲染（exit code）＋產出 PNG 供目視檢查排版。
# 用法：validate.sh <markdown檔> [輸出目錄]（省略則用 mktemp）
set -euo pipefail

doc="$1"
outdir="${2:-$(mktemp -d)}"
mkdir -p "$outdir"

python3 - "$doc" "$outdir" <<'EOF'
import re
import sys
from pathlib import Path

doc = Path(sys.argv[1]).read_text()
outdir = Path(sys.argv[2])
blocks = re.findall(r"```mermaid\n(.*?)```", doc, re.S)
for i, block in enumerate(blocks):
    (outdir / f"diag{i}.mmd").write_text(block)
print(f"{len(blocks)} 個 mermaid 區塊 → {outdir}")
EOF

status=0
for f in "$outdir"/diag*.mmd; do
  png="${f%.mmd}.png"
  err="${f%.mmd}.err"
  if npx --yes @mermaid-js/mermaid-cli -i "$f" -o "$png" --scale 2 --quiet >/dev/null 2>"$err"; then
    echo "OK   $png"
  else
    echo "FAIL $f"
    cat "$err"
    status=1
  fi
done
exit $status
