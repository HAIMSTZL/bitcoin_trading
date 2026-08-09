#!/usr/bin/env bash
# 后台启动交易服务。
# 用法：bash start.sh
# 可选环境变量：WEB_PORT=8001 bash start.sh

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
PORT="${WEB_PORT:-8000}"
HOST="${WEB_HOST:-127.0.0.1}"
LOG_DIR="$PROJECT_DIR/log"
PID_FILE="$LOG_DIR/trading_service.pid"
BOOT_LOG="$LOG_DIR/service_boot.log"

if [[ ! -x "$PYTHON" ]]; then
    echo "未找到项目专用 Python：$PYTHON"
    echo "请先创建虚拟环境并安装依赖。"
    exit 1
fi

if lsof -tiTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "${PORT} 端口已有服务在运行（PID: $(lsof -tiTCP:"${PORT}" -sTCP:LISTEN | tr '\n' ' ')）"
    echo "如需停止，请执行：bash stop.sh ${PORT}"
    exit 1
fi

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

echo "正在后台启动交易服务（模式：${TRADING_MODE:-paper}，端口：${PORT}）..."
nohup "$PYTHON" -u run.py >>"$BOOT_LOG" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PID_FILE"

# 最多等待 10 秒确认 uvicorn 已开始监听；启动失败时给出日志位置。
for _ in $(seq 1 20); do
    if lsof -tiTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "服务已启动：PID ${PID}"
        echo "面板地址：http://${HOST}:${PORT}"
        echo "启动日志：$BOOT_LOG"
        exit 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "服务启动失败，请查看：$BOOT_LOG"
        exit 1
    fi
    sleep 0.5
done

echo "服务进程（PID ${PID}）仍在启动中，请稍后访问：http://${HOST}:${PORT}"
echo "启动日志：$BOOT_LOG"
