#!/usr/bin/env bash
#
# 全新 Ubuntu/Debian 主機上的 container 化部署腳本（見 README「部署模式」）。
# 在已經 clone 好這個 repo 的目標主機上執行：
#
#   cd alice-office-router
#   cp .env.example .env && vim .env   # 先把真的密鑰填好，見下方檢查
#   ./scripts/deploy_host.sh
#
# 冪等：Docker／compose 已裝、network 已建、image 已 build 過都會跳過，
# 可以放心重跑（例如改完 .env 想重新 up 一次）。
#
# 選項：
#   --pull-hermes   跳過 Dockerfile.hermes build，直接用 .env 裡 HERMES_IMAGE
#                   指定的既有 image（例如 pin 版本的官方 nousresearch/hermes-agent）。
#                   local-tools 的 math/OCR/webdriver 與 secretary-mcp 會不能用，
#                   細節見 README「3. 建立 Docker 網路、準備 Hermes image」。
#   --no-verify     部署完不跑 ping 驗證（預設會跑，需要 uv）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NETWORK_NAME="hermes_global_net"
HERMES_IMAGE_TAG="alice-hermes-agent:v1"

PULL_HERMES=false
RUN_VERIFY=true
for arg in "$@"; do
  case "${arg}" in
    --pull-hermes) PULL_HERMES=true ;;
    --no-verify) RUN_VERIFY=false ;;
    *)
      echo "[deploy] 不認得的參數: ${arg}" >&2
      exit 1
      ;;
  esac
done

log() { echo "[deploy] $*"; }

cd "${REPO_ROOT}"

if [[ ! -f .env ]]; then
  log "找不到 .env，先 cp .env.example .env 並填好真的密鑰再重跑（見 README「2. 設定環境變數」）"
  exit 1
fi

if ! grep -qE '^ROUTER_IN_DOCKER=true' .env; then
  log "警告：.env 的 ROUTER_IN_DOCKER 不是 true，container 化部署模式需要它，請確認"
fi

# ---------------------------------------------------------------------------
# 1. Docker + Compose
# ---------------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then
  log "安裝 Docker（apt）..."
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$(whoami)"
  log "已把 $(whoami) 加進 docker 群組——這個 shell session 還沒生效，"
  log "本腳本接下來的 docker 指令會用 sudo 執行；下次重新登入後就能免 sudo 用 docker"
else
  log "Docker 已安裝：$(docker --version)"
fi

DOCKER="sudo docker"
if docker ps >/dev/null 2>&1; then
  # 目前這個 session 本來就能免 sudo 用 docker（例如是新登入的 session）
  DOCKER="docker"
fi

if ${DOCKER} compose version >/dev/null 2>&1; then
  log "Docker Compose 已安裝：$(${DOCKER} compose version --short 2>/dev/null || echo ok)"
else
  log "安裝 docker-compose-v2（apt）..."
  sudo apt-get install -y -qq docker-compose-v2 || sudo apt-get install -y -qq docker-compose-plugin
fi

# ---------------------------------------------------------------------------
# 2. hermes_global_net（docker-compose.yml 宣告為 external，沒先建會直接啟動失敗）
# ---------------------------------------------------------------------------

if ${DOCKER} network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
  log "network ${NETWORK_NAME} 已存在"
else
  log "建立 network ${NETWORK_NAME}"
  ${DOCKER} network create "${NETWORK_NAME}"
fi

# ---------------------------------------------------------------------------
# 3. Hermes agent image
# ---------------------------------------------------------------------------

if ${PULL_HERMES}; then
  log "跳過 build，沿用 .env 裡的 HERMES_IMAGE（記得已經是 pin 死的版本 tag）"
else
  log "build Hermes agent 衍生 image：${HERMES_IMAGE_TAG}（含 plugin/MCP 依賴，需要幾分鐘）"
  ${DOCKER} build -f Dockerfile.hermes -t "${HERMES_IMAGE_TAG}" .
fi

# ---------------------------------------------------------------------------
# 4. Router
# ---------------------------------------------------------------------------

log "docker compose up -d --build"
${DOCKER} compose up -d --build

log "容器狀態："
${DOCKER} ps --filter "name=alice-office-router" --filter "name=hermes_"

# ---------------------------------------------------------------------------
# 5. 驗證（簽章 ping，不建容器不打 LLM，見 scripts/test_webhook.py）
# ---------------------------------------------------------------------------

if ${RUN_VERIFY}; then
  if ! command -v uv >/dev/null 2>&1; then
    log "找不到 uv，安裝中（scripts/test_webhook.py 要用它跑）..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
  fi
  log "等 router 把 port 8000 bind 起來..."
  for _ in $(seq 1 15); do
    if curl -fsS -o /dev/null "http://localhost:8000/docs" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  log "送 ping 驗證 router 是否正常起來..."
  uv run python scripts/test_webhook.py --ping
else
  log "已跳過驗證（--no-verify）"
fi

log "部署完成。接下來：把真實 LINE OA 的 Webhook URL 設成 https://<你的 ngrok/domain>/webhook"
log "看 log：${DOCKER} compose logs -f webhook_router"
