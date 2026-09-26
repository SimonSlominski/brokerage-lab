PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff
DOCKER := $(shell command -v docker 2>/dev/null || echo /Applications/Docker.app/Contents/Resources/bin/docker)

.PHONY: setup db migrate seed run test verify verify-local format lint demo up down reset-demo benchmark
setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements/dev.txt
	$(PYTHON) -m pip install --no-deps -e .
db:
	$(DOCKER) compose up -d --wait db
migrate:
	.venv/bin/alembic upgrade head
seed:
	$(PYTHON) -m brokerage_lab.demo seed
run:
	.venv/bin/uvicorn brokerage_lab.api:create_app --factory --reload --host 127.0.0.1
test:
	$(PYTHON) -m pytest -q
verify:
	$(DOCKER) compose --profile tools run --build --rm tests
verify-local:
	$(RUFF) check .
	$(RUFF) format --check .
	$(PYTHON) -m pytest --postgres -q
format:
	$(RUFF) format .
	$(RUFF) check --fix .
lint:
	$(RUFF) check .
	$(RUFF) format --check .
demo up:
	$(DOCKER) compose up --build -d --wait
	@echo "Panel: http://127.0.0.1:8000/lab (operator / OPERATOR_API_KEY from .env)"
down:
	$(DOCKER) compose down
reset-demo:
	$(PYTHON) -m brokerage_lab.demo reset --confirm
benchmark:
	$(PYTHON) -m brokerage_lab.benchmark
