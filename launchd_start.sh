#!/bin/bash
# launchd 包装脚本：由 launchd 调用，启动数字分身守护进程
# 不用 nohup，因为 launchd 自己就是进程管理者
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
exec "$DIR/.venv/bin/python" "$DIR/agent.py"
