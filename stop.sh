#!/bin/bash
# 停止数字分身守护进程
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ ! -f state/bot.pid ]; then
  echo "未找到 PID 文件，尝试按进程名清理…"
  pkill -f "digital-twin/agent.py" && echo "✅ 已停止" || echo "没有运行中的进程"
  exit 0
fi

PID="$(cat state/bot.pid)"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "✅ 已发送停止信号给 PID $PID"
  for i in $(seq 1 10); do
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "进程未响应，强制结束…"
    kill -9 "$PID" 2>/dev/null || true
  fi
else
  echo "进程 $PID 已不在运行"
fi
rm -f state/bot.pid
echo "已清理 PID 文件"
