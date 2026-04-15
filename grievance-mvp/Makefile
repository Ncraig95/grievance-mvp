ENV_FILE := .env
COMPOSE := docker compose --env-file $(ENV_FILE)
WATCHDOG_STATE_DIR := ./data/watchdog
WATCHDOG_DISABLE_FILE := $(WATCHDOG_STATE_DIR)/disable_auto_restart

.PHONY: up down restart ps logs config pull cloudflare sync-docuseal-url sync-docuseal-webhook \
	smoke smoke-signed verify-download install-systemd watchdog-check watchdog-disable watchdog-enable watchdog-status \
	outreach-run-due

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
	./bring-cloudflare-live.sh

sync-docuseal-url:
	./sync-docuseal-public-url.sh

sync-docuseal-webhook:
	./sync-docuseal-webhook.sh

smoke:
	./scripts/smoke-e2e.sh

smoke-signed:
	./scripts/smoke-signed-intake.sh

verify-download:
	./scripts/verify-docuseal-download.sh

outreach-run-due:
	./scripts/run-outreach-due.sh

install-systemd:
	sudo ./scripts/install-systemd-services.sh

watchdog-check:
	./scripts/watchdog-restart.sh

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
	@systemctl --no-pager --full status grievance-mvp-outreach-due.timer || true
