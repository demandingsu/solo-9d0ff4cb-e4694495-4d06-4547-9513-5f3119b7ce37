#!/bin/sh
set -e

# 初始化/升级 SQLite 表结构（幂等），再启动 Web 进程
mkdir -p "$(dirname "$LABEL_DB_PATH")"
python -m app.db

exec "$@"
