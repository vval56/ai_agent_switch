# 🤖 AI Agent Switch

AI-агент для диагностики и настройки сетевого оборудования (Zyxel, Cudy, MikroTik) через естественный язык. Агент подключается к устройствам по SSH, читает конфигурацию и логи, ищет ответы в PDF-мануалах (RAG) и присылает уведомления об ошибках в Telegram.

![Python](https://img.shields.io/badge/Python-3.14-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green)
![Docker](https://img.shields.io/badge/Docker-ready-blue)
![LLM](https://img.shields.io/badge/LLM-Llama%203.3%2070B-orange)

---

## ✨ Возможности

- 💬 **Чат с инженером** — веб-интерфейс на WebSocket. Пишешь по-русски: «покажи конфигурацию», «настрой firewall», «что в логах?» — агент делает.
- 🔌 **SSH-управление оборудованием**:
  - **Zyxel / Cudy** (ZyNOS) — `show vlan`, `show interface`, `show config` и др.
  - **MikroTik RouterOS** — `/interface print`, `/ip route print`, `/export`, настройка firewall и т.д.
- 🛡️ **Безопасность** — read/write инструменты разделены. Write-операции требуют явного подтверждения пользователя. Опасные команды (`erase`, `reload`, `reset`, `format`…) блокируются политиками.
- 📚 **RAG по PDF-мануалам** — загружаешь PDF мануал, он индексируется в ChromaDB, и агент отвечает по его содержимому с указанием страниц.
- 🔔 **Telegram-уведомления** — ошибки диагностики, изменения конфигурации и плохие логи приходят в Telegram.
- 📡 **Мониторинг логов** — фоновый сервис слушает syslog (UDP 1514) и опрашивает устройства по SSH, шлёт алерты при ошибках.
- 🧠 **Долговременная память** — устройства, история диагностик и история чата сохраняются в `.agent_memory.json`.
- 🔧 **MCP-серверы** — инструменты реализованы как Model Context Protocol серверы (filesystem, switch-diag, mikrotik-diag, memory-manager, pdf-reader).

---

## 🏗️ Архитектура

```
┌─────────────────────────────────────────────────────────┐
│                    Веб-интерфейс (static/)                │
│                  WebSocket ←→ FastAPI                     │
├─────────────────────────────────────────────────────────┤
│                 LangChain Agent (Llama 3.3 70B)           │
│              через NVIDIA / OpenAI-compatible API         │
├──────────────┬──────────────┬────────────┬───────────────┤
│  switch-diag │ mikrotik-diag│  memory-mgr│  rag-search   │
│   (MCP)      │    (MCP)     │   (MCP)    │  (ChromaDB)   │
├──────────────┴──────────────┴────────────┴───────────────┤
│            netmiko (SSH)  ·  Telegram  ·  Syslog          │
└─────────────────────────────────────────────────────────┘
```

### Структура проекта

```
ai-agent-switch/
├── src/
│   ├── agent/
│   │   ├── main.py          # FastAPI + WebSocket + LangChain агент
│   │   └── prompts.py       # Системный промпт инженера
│   ├── mcp_servers/
│   │   ├── switch_diag.py   # SSH-инструменты Zyxel/Cudy
│   │   ├── mikrotik_diag.py # SSH-инструменты MikroTik RouterOS
│   │   ├── memory_manager.py# Память: устройства, история, подключение
│   │   ├── rag_search.py    # RAG-поиск по PDF (MCP-версия)
│   │   └── pdf_reader.py    # Чтение PDF
│   ├── utils/
│   │   ├── memory.py        # JSON-память + SSH-подключения (netmiko)
│   │   ├── telegram.py      # Уведомления в Telegram
│   │   └── monitor.py       # Фоновый мониторинг логов
│   └── monitor_service.py   # Syslog-сервер + SSH-поллинг
├── static/index.html        # Веб-фронтенд
├── mcp_config.json          # Конфиг MCP-серверов
├── index_pdf.py             # CLI-индексация PDF
├── docker-compose.yml       # Docker: agent + nginx
├── Dockerfile
├── Makefile                 # Команды сборки/деплоя
├── deploy.sh                # Скрипт деплоя с healthcheck
└── .env.example             # Пример конфигурации
```

---

## 🚀 Быстрый старт

### Требования

| Спецификация | Минимум | Рекомендуется |
|-------------|---------|---------------|
| RAM | 4 GB | 8 GB |
| CPU | 2 cores | 4 cores |
| Disk | 10 GB | 20 GB |
| Docker | 20.10+ | latest |

### 1. Клонирование

```bash
git clone https://github.com/vval56/ai_agent_switch.git
cd ai_agent_switch
```

### 2. Конфигурация

```bash
cp .env.example .env
```

Заполни `.env`:

| Переменная | Обязательно | Описание |
|-----------|------------|----------|
| `NVIDIA_API_KEY` | ✅ | Ключ для LLM API (NVIDIA NIM / OpenAI-совместимый) |
| `NVIDIA_BASE_URL` | ❌ | URL API (дефолт: `https://integrate.api.nvidia.com/v1`) |
| `NVIDIA_MODEL_NAME` | ❌ | Модель (дефолт: `meta/llama-3.3-70b-instruct`) |
| `TELEGRAM_BOT_TOKEN` | ❌ | Токен бота для уведомлений |
| `TELEGRAM_CHAT_ID` | ❌ | Числовой ID чата для уведомлений |
| `SYSLOG_ENABLED` | ❌ | Приём syslog по UDP (дефолт: `true`) |
| `SYSLOG_PORT` | ❌ | Порт syslog (дефолт: `1514`) |
| `SSH_POLL_ENABLED` | ❌ | SSH-поллинг логов (дефолт: `true`) |
| `MONITOR_POLL_INTERVAL` | ❌ | Интервал опроса, сек (дефолт: `60`) |

### 3. Запуск через Docker

```bash
make deploy
```

Проверка:

```bash
curl http://localhost:8000/api/health
# {"status":"ok","tools_count":28}
```

Открой `http://localhost:8000` в браузере.

### 3. Локальный запуск (без Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

---

## 📖 Использование

### Чат

Открой веб-интерфейс, добавь устройство (IP, логин, пароль, тип), выбери его и пиши агенту:

- **«покажи конфигурацию»** — агент подключится по SSH и покажет текущие настройки
- **«покажи логи»** — выведет логи устройства
- **«настрой firewall: открой SSH и Winbox, остальное дропни»** — агент составит план команд, попросит подтверждение и применит
- **«как настроить VLAN 16?»** — агент найдёт ответ в проиндексированных PDF-мануалах

### Индексация PDF-мануалов

Через веб-интерфейс (кнопка загрузки) или через CLI:

```bash
make index-pdf FILE=docs/manual.pdf
# или напрямую:
python index_pdf.py docs/manual.pdf
```

### Команды Make

```bash
make build       # Сборка Docker-образа
make up          # Запуск сервисов
make down        # Остановка
make restart     # Перезапуск агента
make logs        # Логи (tail -f)
make shell       # Shell в контейнере
make backup      # Бэкап chroma_db + памяти
make index-pdf FILE=docs/manual.pdf  # Индексация PDF
make prod        # Деплой с nginx reverse proxy
make clean       # Полная очистка контейнеров и образов
```

---

## 🛡️ Безопасность

- **Read/Write разделение** — диагностические команды (`show`, `print`, `ping`) и команды настройки (`apply_*`) реализованы разными инструментами.
- **Подтверждение операций** — write-инструменты требуют `confirm=true`, который агент ставит только после явного «да» от пользователя.
- **Политики команд** — настраиваемые списки разрешённых (`readonly_prefixes`) и заблокированных (`blocked_patterns`) команд. Управляются через `/api/policies`.
- **Дедупликация** — повторное выполнение той же команды на том же устройстве блокируется автоматически.
- **Запрет симуляций** — системный промпт категорически запрещает агенту выдумывать вывод команд.

---

## 📡 Мониторинг

### Syslog

Настрой отправку syslog на порт `1514/udp` машины с агентом. Для Zyxel GS1920:

```
GS1920> enable
GS1920# configure terminal
GS1920(config)# logging host <IP_МАШИНЫ_С_АГЕНТОМ>
GS1920(config)# logging on
GS1920(config)# exit
GS1920# write memory
```

Подробности: [`docs/SYSLOG_SETUP.md`](docs/SYSLOG_SETUP.md).

### SSH-поллинг

Агент периодически (каждые 60 сек по умолчанию) подключается к сохранённым устройствам, читает логи и при обнаружении ошибок (`error`, `warn`, `fail`, `link down`, `auth`…) шлёт алерт в Telegram. Дубликаты фильтруются по хешу.

---

## 🔄 API

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/health` | Статус агента и количество инструментов |
| `GET` | `/api/devices` | Список сохранённых устройств |
| `POST` | `/api/devices` | Добавить устройство |
| `DELETE` | `/api/devices/{id}` | Удалить устройство |
| `POST` | `/api/upload` | Загрузить PDF (автоиндексация в RAG) |
| `GET` | `/api/documents` | Список проиндексированных документов |
| `GET` | `/api/policies` | Политики команд |
| `POST` | `/api/policies/{key}` | Установить политику |
| `GET` | `/api/chat_history/{device_id}` | История чата по устройству |
| `WS` | `/ws` | WebSocket для чата с агентом |

---

## 🧰 Технологии

- **LLM**: Llama 3.3 70B (через NVIDIA NIM / OpenAI-совместимый API)
- **Agent framework**: LangChain + LangChain MCP Adapters
- **MCP**: Model Context Protocol (stdio transport)
- **SSH**: netmiko (Zyxel ZyNOS, MikroTik RouterOS, Cisco IOS)
- **RAG**: ChromaDB + sentence-transformers (all-MiniLM-L6-v2) + PyMuPDF
- **Backend**: FastAPI + uvicorn + WebSocket
- **Уведомления**: Telegram Bot API (httpx)
- **Деплой**: Docker + docker-compose + nginx

---

## 📦 Деплой в production

С nginx reverse proxy и SSL:

```bash
mkdir -p ssl
# положи fullchain.pem и privkey.pem в ./ssl/
make prod
```

Подробное руководство: [`DEPLOY.md`](DEPLOY.md).

---

## 📝 Лицензия

См. репозиторий проекта.

---

## 👤 Автор

**vval56** — [github.com/vval56](https://github.com/vval56)
