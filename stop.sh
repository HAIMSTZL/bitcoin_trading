#!/bin/bash
# 停止占用 8000 端口的交易服务。
# 用法: bash stop.sh
# 默认优雅结束(SIGTERM)，进程不退出再强杀(SIGKILL)。

PORT=8000

pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN)

if [ -z "$pids" ]; then
    echo "8000 端口没有正在运行的服务"
    exit 0
fi

echo "发现 8000 端口服务进程: $pids"
kill $pids 2>/dev/null

# 最多等待 5 秒优雅退出
for i in 1 2 3 4 5; do
    sleep 1
    pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN)
    if [ -z "$pids" ]; then
        echo "服务已停止"
        exit 0
    fi
done

echo "优雅退出超时，强制结束: $pids"
kill -9 $pids 2>/dev/null
sleep 1

if [ -z "$(lsof -tiTCP:$PORT -sTCP:LISTEN)" ]; then
    echo "服务已停止"
else
    echo "停止失败，请手动执行: lsof -iTCP:$PORT -sTCP:LISTEN"
    exit 1
fi
