
.PHONY: up down ps logs

# Get the directory of the currently running script
# and set the project directory relative to that.
# This ensures that the docker-compose commands are always
# run from the correct context.
PROJECT_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))

up:
	docker-compose -f $(PROJECT_DIR)/docker-compose.yml up -d --build

down:
	docker-compose -f $(PROJECT_DIR)/docker-compose.yml down

ps:
	docker-compose -f $(PROJECT_DIR)/docker-compose.yml ps

logs:
	docker-compose -f $(PROJECT_DIR)/docker-compose.yml logs -f

