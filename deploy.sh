#!/bin/bash
set -e

echo "=========================================="
echo "  AI Agent Switch - Deploy Script"
echo "=========================================="

# Проверки
if ! command -v docker &> /dev/null; then
    echo "❌ Docker не установлен. Установите: https://docs.docker.com/get-docker/"
    exit 1
fi

if [ ! -f .env ]; then
    echo "⚠️  Файл .env не найден. Копирую из .env.example..."
    cp .env.example .env
    echo "⚠️  ЗАПОЛНИТЕ .env ПЕРЕД ЗАПУСКОМ!"
    exit 1
fi

echo "🛑 Останавливаю старые контейнеры..."
docker compose down --remove-orphans 2>/dev/null || true

echo "🧹 Очищаю старые образы..."
docker image prune -f --filter "until=24h" || true

echo "🔨 Собираю образ..."
docker compose build --no-cache

echo "🚀 Запускаю сервисы..."
docker compose up -d

echo "⏳ Жду запуска (60 сек)..."
sleep 60

echo "🏥 Проверяю здоровье..."
if curl -sf http://localhost:8000/api/health > /dev/null; then
    echo "✅ Сервис запущен и доступен на http://localhost:8000"
else
    echo "❌ Сервис не отвечает. Логи:"
    docker compose logs --tail 50 agent
    exit 1
fi

echo ""
echo "=========================================="
echo "  📋 Информация о развертывании"
echo "=========================================="
echo "Frontend:  http://$(hostname -I | awk '{print $1}'):8000"
echo "API:       http://$(hostname -I | awk '{print $1}'):8000/api"
echo "WebSocket: ws://$(hostname -I | awk '{print $1}'):8000/ws"
echo "Syslog:    udp://$(hostname -I | awk '{print $1}'):1514"
echo ""
echo "Полезные команды:"
echo "  docker compose logs -f agent   — логи агента"
echo "  docker compose logs -f         — все логи"
echo "  docker compose restart agent   — перезапуск"
echo "  docker compose down            — остановка"
echo "=========================================="
