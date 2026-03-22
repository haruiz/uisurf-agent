UV ?= uv
CLI := $(UV) run uisurf_agent run

HOST ?= 127.0.0.1
BROWSER_PORT ?= 8001
DESKTOP_PORT ?= 8002
HEADLESS ?= false
FAST_MODE ?= true
INCLUDE_THOUGHTS ?= false
MAX_OBSERVATION_IMAGES ?= 2
OBSERVATION_SCALE ?= 0.75
DESKTOP_OBSERVATION_DELAY_MS ?= 750

TASK ?=
BROWSER_TASK ?=
DESKTOP_TASK ?=
BROWSER_TASK_DEFAULT ?= Open example.com and summarize the page
DESKTOP_TASK_DEFAULT ?= Open Terminal and run pwd
MAX_STEPS ?= 20
AUTO_CONFIRM ?= false

.DEFAULT_GOAL := help
.PHONY: help run run-both run-browser run-desktop run-browser-interactive run-desktop-interactive

help:
	@printf '%s\n' \
		'make run-browser HOST=127.0.0.1 BROWSER_PORT=8001' \
		'make run-desktop HOST=127.0.0.1 DESKTOP_PORT=8002' \
		'make run-both HOST=127.0.0.1 BROWSER_PORT=8001 DESKTOP_PORT=8002' \
		'make run-browser-interactive TASK="Open example.com and summarize the page"' \
		'make run-desktop-interactive TASK="Open Terminal and run pwd"'

run: run-both

run-browser:
	@if lsof -nP -iTCP:$(BROWSER_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Port $(BROWSER_PORT) is already in use. Stop the existing process or rerun with BROWSER_PORT=<free-port>."; \
		exit 1; \
	fi
	$(CLI) browser_agent \
		--mode a2a \
		--host $(HOST) \
		--port $(BROWSER_PORT) \
		$(if $(filter true,$(FAST_MODE)),--fast-mode,--no-fast-mode) \
		$(if $(filter true,$(INCLUDE_THOUGHTS)),--include-thoughts,--no-include-thoughts) \
		--max-observation-images $(MAX_OBSERVATION_IMAGES) \
		--observation-scale $(OBSERVATION_SCALE)

run-desktop:
	@if lsof -nP -iTCP:$(DESKTOP_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Port $(DESKTOP_PORT) is already in use. Stop the existing process or rerun with DESKTOP_PORT=<free-port>."; \
		exit 1; \
	fi
	$(CLI) desktop_agent \
		--mode a2a \
		--host $(HOST) \
		--port $(DESKTOP_PORT) \
		--desktop-observation-delay-ms $(DESKTOP_OBSERVATION_DELAY_MS) \
		$(if $(filter true,$(INCLUDE_THOUGHTS)),--include-thoughts,--no-include-thoughts) \
		--max-observation-images $(MAX_OBSERVATION_IMAGES) \
		--observation-scale $(OBSERVATION_SCALE)

run-both:
	@if lsof -nP -iTCP:$(BROWSER_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Port $(BROWSER_PORT) is already in use. Stop the existing process or rerun with BROWSER_PORT=<free-port>."; \
		exit 1; \
	fi
	@if lsof -nP -iTCP:$(DESKTOP_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Port $(DESKTOP_PORT) is already in use. Stop the existing process or rerun with DESKTOP_PORT=<free-port>."; \
		exit 1; \
	fi
	@browser_pid=''; \
	desktop_pid=''; \
	trap 'if [ -n "$$browser_pid" ]; then kill "$$browser_pid" 2>/dev/null || true; fi; if [ -n "$$desktop_pid" ]; then kill "$$desktop_pid" 2>/dev/null || true; fi; wait "$$browser_pid" 2>/dev/null || true; wait "$$desktop_pid" 2>/dev/null || true; exit 130' INT TERM; \
	$(CLI) browser_agent \
		--mode a2a \
		--host $(HOST) \
		--port $(BROWSER_PORT) \
		$(if $(filter true,$(FAST_MODE)),--fast-mode,--no-fast-mode) \
		$(if $(filter true,$(INCLUDE_THOUGHTS)),--include-thoughts,--no-include-thoughts) \
		--max-observation-images $(MAX_OBSERVATION_IMAGES) \
		--observation-scale $(OBSERVATION_SCALE) & \
	browser_pid=$$!; \
	$(CLI) desktop_agent \
		--mode a2a \
		--host $(HOST) \
		--port $(DESKTOP_PORT) \
		--desktop-observation-delay-ms $(DESKTOP_OBSERVATION_DELAY_MS) \
		$(if $(filter true,$(INCLUDE_THOUGHTS)),--include-thoughts,--no-include-thoughts) \
		--max-observation-images $(MAX_OBSERVATION_IMAGES) \
		--observation-scale $(OBSERVATION_SCALE) & \
	desktop_pid=$$!; \
	wait $$browser_pid; \
	browser_status=$$?; \
	wait $$desktop_pid; \
	desktop_status=$$?; \
	test $$browser_status -eq 0 && test $$desktop_status -eq 0

run-browser-interactive:
	$(CLI) browser_agent \
		--task "$(or $(BROWSER_TASK),$(TASK),$(BROWSER_TASK_DEFAULT))" \
		--max-steps $(MAX_STEPS) \
		$(if $(filter true,$(HEADLESS)),--headless,) \
		$(if $(filter true,$(AUTO_CONFIRM)),--auto-confirm,) \
		$(if $(filter true,$(FAST_MODE)),--fast-mode,--no-fast-mode) \
		$(if $(filter true,$(INCLUDE_THOUGHTS)),--include-thoughts,--no-include-thoughts) \
		--max-observation-images $(MAX_OBSERVATION_IMAGES) \
		--observation-scale $(OBSERVATION_SCALE)

run-desktop-interactive:
	$(CLI) desktop_agent \
		--task "$(or $(DESKTOP_TASK),$(TASK),$(DESKTOP_TASK_DEFAULT))" \
		--max-steps $(MAX_STEPS) \
		$(if $(filter true,$(AUTO_CONFIRM)),--auto-confirm,) \
		--desktop-observation-delay-ms $(DESKTOP_OBSERVATION_DELAY_MS) \
		$(if $(filter true,$(INCLUDE_THOUGHTS)),--include-thoughts,--no-include-thoughts) \
		--max-observation-images $(MAX_OBSERVATION_IMAGES) \
		--observation-scale $(OBSERVATION_SCALE)
