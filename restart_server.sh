#!/usr/bin/env bash
# 优雅重启后台交易服务。
# 用法：bash restart_server.sh [端口]
# 示例：WEB_PORT=8001 bash restart_server.sh
#
# 若端口已有服务：复用 stop.sh 发送 SIGTERM，等待 run.py 停止各策略线程、
# 最终保存模拟盘状态并关闭 SQLite；确认端口释放后再交给 start.sh 后台拉起。

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-${WEB_PORT:-8000}}"

if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "端口必须是 1 到 65535 的整数，当前值：$PORT" >&2
    exit 2
fi

if lsof -tiTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    PIDS="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN | tr '\n' ' ')"
    echo "检测到 ${PORT} 端口正在运行的服务（PID: ${PIDS}）"
    echo "正在优雅停止并保存策略状态…"
    bash "$PROJECT_DIR/stop.sh" "$PORT"

    # stop.sh 成功才会返回；再次确认是为了避免后续启动与残留监听竞争端口。
    if lsof -tiTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "服务仍占用 ${PORT} 端口，已取消重启。" >&2
        exit 1
    fi
else
    echo "${PORT} 端口当前没有运行中的服务，直接启动。"
fi

echo "正在启动交易服务…"
WEB_PORT="${PORT}" bash "$PROJECT_DIR/start.sh"
