# AI Agent Switch - Deployment Guide

## Быстрый деплой (Docker)

### 1. Подготовка сервера

```bash
# Ubuntu 22.04/24.04
sudo apt update && sudo apt install -y docker.io docker-compose git
sudo systemctl enable --now docker
```

### 2. Клонирование

```bash
git clone <your-repo-url>
cd ai-agent-switch
```

### 3. Конфигурация

```bash
cp .env.example .env
nano .env  # заполни NVIDIA_API_KEY и опционально Telegram
```

### 4. Запуск

```bash
make deploy
# или вручную:
docker compose up -d
```

### 5. Проверка

```bash
curl http://localhost:8000/api/health
# Должно вернуть: {"status":"ok","tools_count":28}
```

Открой браузер: `http://<server-ip>:8000`

---

## Docker Compose команды

```bash
make build      # Сборка образа
make up         # Запуск
make down       # Остановка
make restart    # Перезапуск агента
make logs       # Логи (tail -f)
make shell      # Shell в контейнере
make clean      # Полная очистка
make backup     # Бэкап chroma_db + память
make index-pdf FILE=docs/manual.pdf  # Индексация PDF
```

---

## Production deployment (с nginx)

```bash
# Настрой SSL-сертификаты в ./ssl/
mkdir -p ssl
# положите fullchain.pem и privkey.pem в ./ssl/

make prod
# или:
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Сайт будет доступен на `http://<server-ip>` (порт 80) с автоматическим проксированием на агент.

---

## Обновление

```bash
git pull
make build
make restart
```

---

## Бэкап

```bash
make backup
# или вручную:
docker compose down
tar -czf backup.tar.gz chroma_db .agent_memory.json
docker compose up -d
```

---

## Требования к серверу

| Спецификация | Минимум | Рекомендуется |
|-------------|---------|---------------|
| RAM | 4 GB | 8 GB |
| CPU | 2 cores | 4 cores |
| Disk | 10 GB | 20 GB |
| Docker | 20.10+ | latest |

## Переменные окружения

| Переменная | Обязательно | Описание |
|-----------|------------|----------|
| `NVIDIA_API_KEY` | ✅ | Ключ для LLM API |
| `NVIDIA_MODEL_NAME` | ❌ | Модель (дефолт: llama-3.3-nemotron-49b) |
| `TELEGRAM_BOT_TOKEN` | ❌ | Токен бота для уведомлений |
| `TELEGRAM_CHAT_ID` | ❌ | ID чата для уведомлений |
| `PORT` | ❌ | Порт фронта (дефолт: 8000) |
| `DOCS_DIR` | ❌ | Путь к PDF мануалам (дефолт: /app/docs) |
