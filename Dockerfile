FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    MCP_SERVER_TIMEOUT=30

WORKDIR /app

# Системные зависимости для PDF/SSH/MCP (npx для filesystem)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    openssh-client \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем только PYTHONPATH код (чтобы не тащить лишнее)
COPY src/ ./src/
COPY static/ ./static/
COPY mcp_config.json ./

EXPOSE 8000 1514/udp

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["python3", "-m", "src.agent.main"]
