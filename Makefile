ENV_FILE := grievance-mvp/.env
COMPOSE := docker compose --env-file $(ENV_FILE)

.PHONY: up down restart ps logs config pull cloudflare sync-docuseal-url smoke smoke-signed verify-download

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
	./grievance-mvp/bring-cloudflare-live.sh

sync-docuseal-url:
	./grievance-mvp/sync-docuseal-public-url.sh

smoke:
	./grievance-mvp/scripts/smoke-e2e.sh

smoke-signed:
	./grievance-mvp/scripts/smoke-signed-intake.sh

verify-download:
	./grievance-mvp/scripts/verify-docuseal-download.sh
