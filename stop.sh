#!/bin/bash
# 安全停止占用 8000 端口的交易服务。
# 用法: bash stop.sh [端口]
#
# 停止流程：SIGTERM → uvicorn 优雅退出 → run.py 收尾逐个调用引擎 stop()
# （先停交易线程，再把网格/持仓状态最终落盘到 SQLite，最后关闭存储）。
# 交易记录/权益/状态本就逐笔实时提交，落盘只是补齐最后一 tick。
# 给足 20 秒优雅退出时间，超时才强杀(SIGKILL)。

PORT=${1:-8000}

pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN)

if [ -z "$pids" ]; then
    echo "$PORT 端口没有正在运行的服务"
    exit 0
fi

echo "发现 $PORT 端口服务进程: $pids"
echo "发送 SIGTERM，等待引擎落盘并优雅退出（最多 20 秒）..."
kill $pids 2>/dev/null

for i in $(seq 1 20); do
    sleep 1
    pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN)
    if [ -z "$pids" ]; then
        echo "服务已安全停止，状态已保存"
        exit 0
    fi
done

echo "优雅退出超时，强制结束: $pids（极端情况最多丢失最后一 tick 的网格状态）"
kill -9 $pids 2>/dev/null
sleep 1

if [ -z "$(lsof -tiTCP:$PORT -sTCP:LISTEN)" ]; then
    echo "服务已停止"
else
    echo "停止失败，请手动执行: lsof -iTCP:$PORT -sTCP:LISTEN"
    exit 1
fi
