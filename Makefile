ENV_FILE := grievance-mvp/.env
COMPOSE := docker compose --env-file $(ENV_FILE)
WATCHDOG_STATE_DIR := ./grievance-mvp/data/watchdog
WATCHDOG_DISABLE_FILE := $(WATCHDOG_STATE_DIR)/disable_auto_restart

.PHONY: up down restart ps logs config pull cloudflare sync-docuseal-url sync-docuseal-webhook \
	smoke smoke-signed verify-download install-systemd watchdog-check watchdog-disable watchdog-enable watchdog-status

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

sync-docuseal-webhook:
	./grievance-mvp/sync-docuseal-webhook.sh

smoke:
	./grievance-mvp/scripts/smoke-e2e.sh

smoke-signed:
	./grievance-mvp/scripts/smoke-signed-intake.sh

verify-download:
	./grievance-mvp/scripts/verify-docuseal-download.sh

install-systemd:
	sudo ./grievance-mvp/scripts/install-systemd-services.sh

watchdog-check:
	./grievance-mvp/scripts/watchdog-restart.sh

watchdog-disable:
	mkdir -p "$(WATCHDOG_STATE_DIR)"
	touch "$(WATCHDOG_DISABLE_FILE)"
	@echo "auto-restart disabled at $(WATCHDOG_DISABLE_FILE)"

watchdog-enable:
	rm -f "$(WATCHDOG_DISABLE_FILE)"
	@echo "auto-restart enabled"

watchdog-status:
	@echo "disable file: $(WATCHDOG_DISABLE_FILE)"
	@if [ -f "$(WATCHDOG_DISABLE_FILE)" ]; then echo "auto-restart: DISABLED"; else echo "auto-restart: ENABLED"; fi
	@systemctl --no-pager --full status grievance-mvp.service || true
	@systemctl --no-pager --full status grievance-mvp-watchdog.timer || true
