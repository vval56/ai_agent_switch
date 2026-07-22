#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "🛑 Останавливаю старые процессы на 8000..."
lsof -ti :8000 | xargs kill -9 2>/dev/null || true
sleep 1

echo "🚀 Запускаю агента..."
.venv/bin/python -m src.agent.main
