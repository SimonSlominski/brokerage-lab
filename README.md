# Brokerage Lab

A local proof of concept for reliable investment order processing. The current increment includes an immutable Pydantic domain model, a local FastAPI order demo, SQLAlchemy mappings and PostgreSQL migrations.

The main question is: **Did the execution partner accept the order?** A lost response must not become a fabricated rejection or release reserved cash.

## Current behavior

- Money uses Decimal and EUR cents; invalid inputs are rejected.
- Account and Order enforce reservation and state transition rules through immutable domain operations.
- PostgreSQL contains accounts, instruments, orders and cash reservations, created through Alembic.
- The demo account starts with 1000.00 EUR; the synthetic instrument costs 100.00 EUR per whole unit.
- FastAPI exposes health checks, the known demo account, local order submission and order reads. Order creation reserves cash atomically; multi-client authentication and request idempotency remain future work.
- Worker, external partner simulation, ledger and reconciliation remain planned components.

## Start the entire application

With Docker Desktop running, execute this command from the repository root:

```bash
docker compose up --build
```

This is the complete startup workflow. No host Python installation, virtual environment, Make command or manual migration command is needed.

Compose runs two containers together: PostgreSQL and the API. After PostgreSQL becomes healthy, the API entrypoint applies Alembic migrations, seeds missing demo data and starts Uvicorn. A migration or seed failure stops startup. Existing balances are preserved by the seed. This startup migration approach is intended for this single local API instance.

The existing .env supplies local credentials automatically. Do not overwrite it with .env.example. For a new clone on another machine, create .env once using the example as a template. No AWS services are needed. Configuration is supplied at runtime, not baked into the image.

Open [API documentation](http://127.0.0.1:8000/docs), [database readiness](http://127.0.0.1:8000/health/ready), or [demo cash](http://127.0.0.1:8000/demo/account).

To run in the background instead:

```bash
docker compose up --build -d
```

To stop the stack while preserving the database:

```bash
docker compose down
```

## Local order demo

In Swagger, submit POST /demo/orders with:

```json
{"quantity": 8}
```

For a fresh 1000.00 EUR demo account, expect HTTP 201, PENDING / NOT_SENT and a reservation of 800.00 EUR. GET /demo/account then shows 1000.00 posted, 800.00 reserved and 200.00 available. Follow the response Location header or GET /demo/orders/{order_id} to read the order.

A subsequent order with quantity 3 returns HTTP 409 and "Insufficient available cash", leaving existing records unchanged. Invalid quantities or unsupported currency/instrument/side return 422. The only supported terms are BUY, SYNTH-100, EUR and 100.00 per whole unit. Account IDs and prices cannot be supplied by the client.

This creates a local order, not a partner execution. Requests are not yet idempotent: resending the same request may create another order if funds permit. A database connection failure during commit can leave the outcome unknown; a 503 response is not a definitive business rejection. The account lock serializes concurrent submissions through this service, not arbitrary SQL writers.

## Edit code while Docker is running

Compose mounts src into the API container and Uvicorn reloads after Python source changes. Save a file in PyCharm to reload the API; no image rebuild is needed for source edits. PYTHONPATH points to /app/src so Python imports the mounted source rather than the installed image copy.

Rebuild after changing dependencies or the Dockerfile. Restart the API after adding a migration: source reload alone does not rerun the container entrypoint.

## Alembic migrations

The initial migration is versioned in migrations/versions. Every API container startup runs alembic upgrade head automatically; already applied migrations are not reapplied.

Inspect the current revision:

```bash
docker compose exec api alembic current
```

The migrations directory is also mounted, so generated migration files are saved directly in the repository. With the stack running, generate a migration after changing the SQLAlchemy models:

```bash
docker compose exec api alembic revision --autogenerate -m "Describe the schema change"
```

Review the generated file, then apply it:

```bash
docker compose exec api alembic upgrade head
```

Startup applies existing migrations automatically; it does not generate them. A Python source reload does not apply new migrations.

## Why pyproject.toml exists

pyproject.toml describes the Python package, supported dependencies and test/lint settings. The Dockerfile uses it to install brokerage_lab inside the image. requirements/base.txt pins runtime dependencies only. requirements/dev.txt includes that file and adds pytest, HTTP test dependencies and Ruff. The Docker image installs only runtime dependencies; optional local test setup installs requirements/dev.txt. Neither file requires a separate manual startup step.

## Optional local Python development

Only use this workflow if you want to run or debug Python directly in PyCharm. The existing .venv is already installed on this machine; select .venv/bin/python as its interpreter. On a fresh checkout, make setup creates this environment.

Start the database with docker compose up -d db, then use make migrate, make seed and make run. Stop the Docker API first with docker compose stop api to free port 8000. Use the repository root as your working directory. The local Pydantic Settings configuration reads .env; its DATABASE_URL uses 127.0.0.1:55432 while the container uses db:5432.

On macOS, if Docker is installed but not in the terminal PATH:

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
```

## Tests and database migrations

```bash
# Optional local .venv with requirements/dev.txt installed
make test       # Domain and API tests; PostgreSQL tests explicitly skipped
make verify     # Includes real PostgreSQL tests and lint; requires make db
.venv/bin/alembic check
```

PostgreSQL tests create a unique temporary schema for each test and drop only that schema afterward. They test migration, HTTP reads, domain mapping, rollback, independent connections, constraints and scoped reset. They do not yet prove concurrent order submission or distributed exactly-once effects.

To reset only the known demo account and its dependent orders/reservations:

```bash
make reset-demo
```

Reset requires development mode and explicit confirmation (provided by this Make target). Other accounts are retained. Seed is idempotent and preserves existing account balances.

## Code map

| File | Responsibility |
|---|---|
| `src/brokerage_lab/domain.py` | Immutable Pydantic values, accounts, orders and business operations |
| `src/brokerage_lab/schemas.py` | Public response shapes |
| `src/brokerage_lab/api.py` | FastAPI routes and lifecycle |
| `src/brokerage_lab/db.py` | SQLAlchemy tables and explicit domain mapping |
| `src/brokerage_lab/config.py` | Environment configuration |
| `src/brokerage_lab/demo.py` | Local seed/reset commands |
| `migrations/` | Versioned database schema |
| `tests/` | Domain, HTTP and PostgreSQL evidence |

Domain changes return a new validated instance:

```python
from brokerage_lab.domain import Account, Money, Order

account = Account(id="example", posted_cash=Money.from_text("1000.00"))
order = Order.demo_buy("purchase-1", account.id, 8)
account = account.reserve(order)
order = order.start_send().mark_timeout()
assert account.available_cash == Money.from_text("200.00")
```

Always keep the returned state. Direct field assignment and `model_copy(update=...)` are blocked. Deliberate validation bypasses such as `model_construct`, private methods and Python internals are not supported application entry points. Restoration from trusted persistence validates state but does not prove transition history.

## D04 transaction boundary

The demo account endpoint calls services.py, which reads through repositories.py within a transaction managed by unit_of_work.py. Domain objects remain Pydantic models; ORM rows remain SQLAlchemy classes.

Repository add flushes SQL but never commits. Call uow.commit() explicitly to make writes durable. Leaving the context without commit rolls back, even on normal exit. An exception before commit rolls back all writes in that transaction. An exception after successful commit cannot undo it. Each Unit of Work instance is single-use and closes its session on exit.

The submission service locks the account before checking cash, then inserts the order and reservation in one Unit of Work. Repositories never commit independently. The HTTP demo always uses demo-account and does not expose other accounts. This fixed demo scope is not multi-client authorization.

## First Git checkpoint

Commit source, tests, migrations, Docker configuration, dependency files, and documentation together as a working foundation. `.env`, `.venv`, caches and `.idea` are ignored. Review `git status --short` first. Git author name and email were not configured during setup; set your own identity locally before the first commit (use a verified GitHub email or your GitHub noreply address).

```bash
git config user.name "Your Name"
git config user.email "Your GitHub email or noreply address"
```

```bash
git add .
git diff --cached --check
git diff --cached --stat
git commit -m "Build Pydantic domain and local FastAPI PostgreSQL foundation"
git push -u origin main
```

A commit is a local history checkpoint. Push uploads it to the configured GitHub repository. Local tests and development do not require a push.

