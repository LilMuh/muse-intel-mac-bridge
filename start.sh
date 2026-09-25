#!/usr/bin/env bash
# muse-intel-mac-bridge · 一键启动（在 Mac 上运行）
#   ./start.sh            启动服务
#   ./start.sh --funnel   启动服务，并用 Tailscale Funnel 暴露到公网（HTTPS）
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "只能在 macOS 上运行 / macOS only"; exit 1
fi

PORT="${PORT:-8765}"

# 1. Python 虚拟环境 + 依赖
if [[ ! -d .venv ]]; then
  echo "==> 创建虚拟环境 .venv"
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# 2. token：首次运行自动生成并保存到 .env（权限 600，已在 .gitignore 中）
if [[ ! -f .env ]]; then
  echo "AGENT_TOKEN=$(openssl rand -hex 24)" > .env
  chmod 600 .env
  echo "==> 已生成新 token 并保存到 .env"
fi
set -a; source .env; set +a

# 3. 可选：Tailscale Funnel
if [[ "${1:-}" == "--funnel" ]]; then
  TS="$(command -v tailscale || true)"
  [[ -z "$TS" && -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]] && \
    TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  if [[ -z "$TS" ]]; then
    echo "未找到 tailscale，请先安装：brew install --cask tailscale"; exit 1
  fi
  echo "==> 开启 Tailscale Funnel（端口 $PORT）"
  "$TS" funnel --bg "$PORT"
  "$TS" funnel status || true
fi

echo
echo "------------------------------------------------------------"
echo " Token: $AGENT_TOKEN"
echo " 本地测试："
echo "   curl -H \"Authorization: Bearer $AGENT_TOKEN\" http://127.0.0.1:$PORT/info"
echo " 把 Funnel 地址和 token 发给 agent（见 docs/muse-prompt.md）"
echo " 紧急停止：把鼠标甩到屏幕左上角，或在此窗口按 Ctrl+C"
echo "------------------------------------------------------------"
echo

# 4. 防止休眠，服务退出时自动结束 caffeinate
caffeinate -dis -w $$ &

HOST="${HOST:-127.0.0.1}" PORT="$PORT" exec .venv/bin/python mac_agent_server.py
