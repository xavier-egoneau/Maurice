WORKSPACE ?= /home/egza/Documents/workspace_maurice
PROFILE ?= limited
MSG ?= salut Maurice
PORT ?= 18791

.PHONY: bootstrap setup link-cli install onboard doctor run start stop logs dashboard gateway telegram test help

help:
	@echo "Maurice local commands"
	@echo "  make bootstrap  Install Maurice and launch onboarding"
	@echo "  make setup      Create .venv and install Maurice for development"
	@echo "  make link-cli   Expose maurice in ~/.local/bin"
	@echo "  make install    Check local prerequisites"
	@echo "  make onboard    Create/update a Maurice workspace"
	@echo "  make doctor     Check the workspace"
	@echo "  make run        Run one mock/agent turn"
	@echo "  make start      Start Maurice background services"
	@echo "  make stop       Stop Maurice background services"
	@echo "  make logs       Show recent Maurice logs"
	@echo "  make dashboard  Show the local text dashboard"
	@echo "  make gateway    Start the local gateway"
	@echo "  make telegram   Start Telegram polling"
	@echo "  make test       Run tests"
	@echo ""
	@echo "Variables:"
	@echo "  WORKSPACE=$(WORKSPACE)"
	@echo "  PROFILE=$(PROFILE)"
	@echo "  MSG=$(MSG)"
	@echo "  PORT=$(PORT)"

bootstrap:
	WORKSPACE="$(WORKSPACE)" PROFILE="$(PROFILE)" ./install.sh

setup:
	python3 -m venv .venv
	. .venv/bin/activate && python -m pip install -e ".[dev]"

link-cli:
	ln -sf "$(CURDIR)/.venv/bin/maurice" "$(HOME)/.local/bin/maurice"
	@echo "maurice linked to $(HOME)/.local/bin/maurice"

install:
	. .venv/bin/activate && maurice install

onboard:
	. .venv/bin/activate && maurice onboard --interactive --workspace "$(WORKSPACE)" --permission-profile "$(PROFILE)"

doctor:
	. .venv/bin/activate && maurice doctor --workspace "$(WORKSPACE)"

run:
	. .venv/bin/activate && maurice run --workspace "$(WORKSPACE)" --message "$(MSG)"

start:
	. .venv/bin/activate && maurice start --workspace "$(WORKSPACE)"

stop:
	. .venv/bin/activate && maurice stop --workspace "$(WORKSPACE)"

logs:
	. .venv/bin/activate && maurice logs --workspace "$(WORKSPACE)"

dashboard:
	. .venv/bin/activate && maurice dashboard --workspace "$(WORKSPACE)"

gateway:
	. .venv/bin/activate && maurice gateway serve --workspace "$(WORKSPACE)" --port "$(PORT)"

telegram:
	. .venv/bin/activate && maurice gateway telegram-poll --workspace "$(WORKSPACE)"

test:
	. .venv/bin/activate && pytest -q
