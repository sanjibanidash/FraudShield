COMPOSE      = docker compose
COMPOSE_PROD = docker compose -f docker-compose.yml -f docker-compose.prod.yml
PSQL         = $(COMPOSE) exec -T postgres psql -U fraud -d fraudshield -c

.PHONY: help setup up down restart ps logs test lint train smoke drift-amount drift-type drift-balance drift-off \
        retrain batch-now batch-sample psql clean prod-up

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## create .env and install dev dependencies
	cp -n .env.example .env || true
	pip install -r requirements-dev.txt

up:  ## build + start the whole stack
	$(COMPOSE) up -d --build
	@echo "Dashboard  http://localhost:5000\nAPI docs   http://localhost:8000/docs\nGrafana    http://localhost:3000\nPrometheus http://localhost:9090\nAlertmgr   http://localhost:9093"

down:  ## stop everything (keeps data)
	$(COMPOSE) down

restart:  ## restart app services
	$(COMPOSE) restart api consumer producer monitor batch dashboard

ps:  ## container status
	$(COMPOSE) ps

logs:  ## follow logs (make logs s=consumer)
	$(COMPOSE) logs -f --tail=100 $(s)

test:  ## unit tests
	pytest -v

lint:  ## ruff
	ruff check .

train:  ## train locally without docker (writes ./models)
	python training/train.py

smoke:  ## end-to-end test against the running stack
	bash scripts/smoke_test.sh

drift-amount:  ## inject drift: amounts x3
	$(PSQL) "UPDATE settings SET value='amount_inflation' WHERE key='drift_mode'"
drift-type:  ## inject drift: shift transaction types
	$(PSQL) "UPDATE settings SET value='type_shift' WHERE key='drift_mode'"
drift-balance:  ## inject drift: lower balances
	$(PSQL) "UPDATE settings SET value='balance_shift' WHERE key='drift_mode'"
drift-off:  ## stop drift injection
	$(PSQL) "UPDATE settings SET value='none' WHERE key='drift_mode'"

retrain:  ## force a retrain now (champion vs challenger)
	$(COMPOSE) exec batch python /app/service/batch_job.py --retrain

batch-sample:  ## drop a CSV into data/batch/input for bulk scoring
	python scripts/make_batch_sample.py

batch-now:  ## run the batch job once (bulk score + daily report + retrain check)
	$(COMPOSE) exec batch python /app/service/batch_job.py --once

psql:  ## open a SQL shell
	$(COMPOSE) exec postgres psql -U fraud -d fraudshield

clean:  ## stop and DELETE all data, models and volumes
	$(COMPOSE) down -v

prod-up:  ## (on the VM) start with production overrides
	$(COMPOSE_PROD) up -d
