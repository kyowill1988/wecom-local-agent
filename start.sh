#!/bin/bash
# 启动数字分身守护进程
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

mkdir -p logs state workspace

if [ -f state/bot.pid ] && kill -0 "$(cat state/bot.pid)" 2>/dev/null; then
  echo "⚠️  数字分身已在运行（PID $(cat state/bot.pid)）。如需重启请先执行 ./stop.sh"
  exit 1
fi

nohup .venv/bin/python agent.py >> logs/stdout.log 2>&1 &
PID=$!
echo "$PID" > state/bot.pid
echo "✅ 数字分身已启动，PID $PID"
echo "   日志：tail -f $DIR/logs/bot.log"
sleep 3
tail -n 15 logs/bot.log 2>/dev/null || true
