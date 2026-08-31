# ==============================================================================
# Civico — Voice Civic Complaint Agent (Rasa Skills + Deepgram)
# ==============================================================================

GREEN   := $(shell tput -Txterm setaf 2 2>/dev/null)
YELLOW  := $(shell tput -Txterm setaf 3 2>/dev/null)
BLUE    := $(shell tput -Txterm setaf 4 2>/dev/null)
MAGENTA := $(shell tput -Txterm setaf 5 2>/dev/null)
RED     := $(shell tput -Txterm setaf 1 2>/dev/null)
RESET   := $(shell tput -Txterm sgr0 2>/dev/null)

UV     := $(shell command -v uv 2>/dev/null)
RUN    := uv run
PYTHON := $(RUN) python
RASA   := $(RUN) rasa

-include .env

.DEFAULT_GOAL := help

.PHONY: help check-uv env install verify test train inspect run \
        guard-env reset-db show-demo-data demo-failure clean clean-all

help: ## Show this help message
	@echo ''
	@echo '$(MAGENTA)Civico — Voice Civic Complaint Agent (Rasa Skills + Deepgram)$(RESET)'
	@echo ''
	@echo '$(YELLOW)First-time setup (in order):$(RESET)'
	@echo '  $(GREEN)make install$(RESET)          Install dependencies into .venv (uv)'
	@echo '  $(GREEN)make env$(RESET)              Create .env from .env.example (never overwrites)'
	@echo '  $(GREEN)make verify$(RESET)           Pre-flight check: keys, project, skills, data'
	@echo '  $(GREEN)make test$(RESET)             Run deterministic tool and database tests'
	@echo '  $(GREEN)make train$(RESET)            Build the agent model'
	@echo '  $(GREEN)make inspect$(RESET)          Talk to the agent (voice + text)'
	@echo ''
	@echo '$(YELLOW)Demo:$(RESET)'
	@echo '  $(GREEN)make show-demo-data$(RESET)   Print mock routing and seeded complaints'
	@echo '  $(GREEN)make demo-failure$(RESET)     Run the Inspector with submit forced to fail (on_failure)'
	@echo '  $(GREEN)make reset-db$(RESET)         Reseed the demo civic DB from data/source/'
	@echo ''

check-uv:
	@if [ -z "$(UV)" ]; then \
		echo "$(RED)✗ uv not found.$(RESET)"; \
		echo "$(YELLOW)  Install it:$(RESET) curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		exit 1; \
	fi

env: ## Create .env from .env.example if it does not exist
	@if [ -e .env ]; then \
		echo "$(GREEN)✓ .env already exists — leaving it untouched.$(RESET)"; \
	else \
		cp .env.example .env; \
		echo "$(GREEN)✓ Created .env from .env.example$(RESET)"; \
		echo "$(YELLOW)  Fill in RASA_LICENSE, OPENAI_API_KEY, DEEPGRAM_API_KEY$(RESET)"; \
	fi

install: check-uv ## Install all dependencies into .venv
	@echo "$(BLUE)Installing dependencies with uv...$(RESET)"
	$(UV) sync --prerelease=allow
	@echo "$(GREEN)✓ Dependencies installed.$(RESET)"
	@echo "$(YELLOW)  Next:$(RESET) make env && make verify"

guard-env:
	@if [ ! -e .env ]; then \
		echo "$(RED)✗ No .env file found.$(RESET)"; \
		echo "$(YELLOW)  Run:$(RESET) make env"; \
		exit 1; \
	fi

verify: check-uv ## Run full pre-flight diagnostics
	@$(PYTHON) scripts/verify_setup.py

test: check-uv ## Run deterministic unit and integration tests
	@$(PYTHON) -m unittest discover -s tests -v

train: check-uv guard-env ## Validate and package the agent model
	@echo "$(BLUE)Training the agent...$(RESET)"
	$(RASA) train
	@echo "$(GREEN)✓ Model ready.$(RESET)  Next: $(GREEN)make inspect$(RESET)"

inspect: check-uv guard-env ## Open the Inspector (voice + text)
	@echo "$(MAGENTA)Opening the Inspector — use the mic for voice, or type.$(RESET)"
	$(RASA) inspect

run: check-uv guard-env ## Start the agent API server
	$(RASA) run --enable-api

demo-call: check-uv ## Talk to a running agent from the terminal (needs `make run`)
	@$(PYTHON) scripts/demo_call.py $(ARGS)

show-demo-data: check-uv ## Print mock routing and seeded complaints
	@$(PYTHON) scripts/show_demo_data.py

demo-failure: check-uv guard-env ## Inspector with submit_complaint forced to fail
	@echo "$(YELLOW)submit_complaint will fail — this demonstrates on_failure.$(RESET)"
	CIVIC_FORCE_SUBMIT_FAILURE=1 $(RASA) inspect

reset-db: ## Delete the demo civic DB so it reseeds from data/source/
	@rm -f data/civic.db
	@echo "$(GREEN)✓ Demo civic DB reset — it will reseed on the next tool call.$(RESET)"

clean: ## Remove models, caches, and the generated demo db
	@rm -rf models .rasa logs data/civic.db
	@find . -name '__pycache__' -type d -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	@echo "$(GREEN)✓ Clean complete.$(RESET)"

clean-all: clean ## Also remove the virtualenv
	@rm -rf .venv
	@echo "$(GREEN)✓ Removed .venv — run make install to start over.$(RESET)"
