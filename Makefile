DOCKER_COMMAND = docker compose -f docker-compose.yml
UV = uv run

help:	## Show this help.
	@echo "============================================================"
	@echo "This is a list of available commands for this project."
	@echo "============================================================"
	@fgrep -h "##" $(MAKEFILE_LIST) | fgrep -v fgrep | sed -e 's/\\$$//' | sed -e 's/##//'

build:	## Builds docker image
	$(DOCKER_COMMAND) build --no-cache

run:	## Runs the envionment in detached mode
	$(DOCKER_COMMAND) up -d --force-recreate

up:	## Runs the non-detached environment
	$(DOCKER_COMMAND) up --force-recreate

stop:	## Stops running instance
	$(DOCKER_COMMAND) stop

down:	## Kills running instance
	$(DOCKER_COMMAND) down

test:	## Run the tests.
	$(UV) pytest -v --cov=app

lint:  ## Check lint + format + types (run before pushing)
	$(UV) ruff check
	$(UV) ruff format --check
	$(UV) ty check

format:  ## Auto-fix lint issues and reformat
	$(UV) ruff check --fix
	$(UV) ruff format

check: lint  ## Alias for `make lint` (the full pre-push gate)

duckdb: ## Import Apple Health XML data to a Parquet file for DuckDB
	$(UV) scripts/duckdb_importer.py

duckdb-reset: ## Delete the imported Apple Health DuckDB file (keeps manual_logs.duckdb)
	$(UV) scripts/duckdb_importer.py --reset
