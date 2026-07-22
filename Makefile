.PHONY: help build up down restart logs shell clean deploy prod

help:
	@echo "AI Agent Switch - Make Commands"
	@echo ""
	@echo "  make build      - Build Docker images"
	@echo "  make up         - Start services (docker compose up -d)"
	@echo "  make down       - Stop services"
	@echo "  make restart    - Restart agent"
	@echo "  make logs       - Show logs (tail -f)"
	@echo "  make shell      - Open shell in agent container"
	@echo "  make clean      - Remove containers, volumes, images"
	@echo "  make deploy     - Full deploy (build + up + healthcheck)"
	@echo "  make prod       - Deploy with nginx reverse proxy"
	@echo "  make index-pdf  - Index PDF (usage: make index-pdf FILE=docs/manual.pdf)"
	@echo "  make backup     - Backup chroma_db and .agent_memory.json"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart agent

logs:
	docker compose logs -f agent

shell:
	docker compose exec agent bash

clean:
	docker compose down --volumes --remove-orphans
	docker system prune -f

deploy:
	@./deploy.sh

prod:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

index-pdf:
	@if [ -z "$(FILE)" ]; then echo "Usage: make index-pdf FILE=docs/manual.pdf"; exit 1; fi
	docker compose exec agent python index_pdf.py /app/$(FILE)

backup:
	@echo "📦 Backup..."
	@mkdir -p backup
	@tar -czf backup/chroma_db_$(shell date +%Y%m%d_%H%M%S).tar.gz chroma_db
	@tar -czf backup/memory_$(shell date +%Y%m%d_%H%M%S).tar.gz .agent_memory.json
	@echo "✅ Backup saved to backup/"
