PROJECT_DIR := grievance-mvp
COMPOSE_FILE := $(PROJECT_DIR)/docker-compose.yml
ENV_FILE := $(PROJECT_DIR)/.env
COMPOSE := docker compose -f "$(COMPOSE_FILE)" --env-file "$(ENV_FILE)"

.PHONY: up down restart ps logs config pull cloudflare sync-docuseal-url

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) up -d --build --force-recreate

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=200

config:
	$(COMPOSE) config

pull:
	$(COMPOSE) pull

cloudflare:
	./$(PROJECT_DIR)/bring-cloudflare-live.sh

sync-docuseal-url:
	./$(PROJECT_DIR)/sync-docuseal-public-url.sh

