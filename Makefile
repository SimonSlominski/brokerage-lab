PYTHON := .venv/bin/python
DOCKER := $(shell command -v docker 2>/dev/null || echo /Applications/Docker.app/Contents/Resources/bin/docker)

.PHONY: setup db migrate seed run test verify up down reset-demo
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
	$(PYTHON) -m pytest --postgres -q
	.venv/bin/ruff check src tests migrations
up:
	$(DOCKER) compose up -d --build --wait api
down:
	$(DOCKER) compose down
reset-demo:
	$(PYTHON) -m brokerage_lab.demo reset --confirm
