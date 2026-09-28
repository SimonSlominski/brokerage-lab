# Brokerage Lab

A local failure laboratory for investment order processing: **did the partner execute the order?** It demonstrates what a local transaction can guarantee and what requires a partner contract. Synthetic EUR BUY orders only; no real trading, FX, partial fills or settlement.

## Run

```bash
docker compose up --build
```

Docker Desktop and a private `.env` are required. The existing local `.env` is ready; a new clone needs the values described in `.env.example` once. Compose reads them automatically. There is no host Python or manual migration step.

- [Operator panel](http://127.0.0.1:8000/lab): browser login `operator`, password `OPERATOR_API_KEY` from `.env`.
- [Swagger](http://127.0.0.1:8000/docs): client requests use `X-API-Key`; operator requests use `X-Operator-Key`.
- `/health/live` checks the API process; `/health/ready` checks local database access. Neither promises that the partner is available.

Compose starts the API, worker, partner simulator and two PostgreSQL 17 databases. A separate migration job applies local Alembic migrations and seeds missing accounts before API/worker startup. The partner applies its own migrations. Existing balances and history survive restart. `docker compose down` stops the stack without deleting volumes.

## Try the failures

The panel presents five visual experiments. Each creates an isolated funded account and records a run ID, seed, status and evidence. A streaming walkthrough highlights the fault and recovery on the architecture diagram, alongside observed cash, reservations and share counts. Checkpoints are paced for readability and can be paused, stepped through or replayed without submitting another order. Technical evidence and explicitly labelled IDs remain in expandable details. The trading ticket shows 8 shares × EUR 100 = EUR 800 against EUR 1,000 starting cash. **LEMON / lemon.markets** is a fictional display name for the existing `SYNTH-100` instrument, not a listed stock or real integration. The mismatch experiment has a separate report comparison ticket and submits no buy order.

Scenarios call the partner over HTTP; the partner persists its own orders and reports.

| Scenario | Expected evidence |
|---|---|
| Lost response | UNKNOWN retains 800 EUR; lookup resolves one execution; final cash 200, reservation 0 |
| 20 retries | One client/key produces one order and reservation despite concurrent requests |
| Worker crash | A real subprocess exits after remote commit; lease expiry and lookup recover the same execution |
| 10 executions | Concurrent delivery produces one journal transaction and instrument movement |
| 975 / 950 mismatch | Explicit report fixture creates an OPEN amount discrepancy; cash remains unchanged |

The mismatch is a **controlled report fixture**, not a possible quantity at the fixed 100 EUR unit price. FAILED runs remain visible. Metrics come from database rows, not scripted success counters. Missing-record metrics count unresolved cases and can include repeated reports.

For individual orders, submit `POST /demo/orders` with `{"quantity":8}`, `X-API-Key` and a new `Idempotency-Key`. A new account starts with 1000 EUR. Reservation gives 1000 posted / 800 reserved / 200 available; execution gives 200 / 0 / 200. The background worker may finish before your next read.

Same client/key and normalized payload replays the original 201 response, even after execution. Read the returned order URL for current state. Changed payload under that key returns 422. Different keys mean distinct intentions. Keys have no automatic expiry in this MVP. Foreign accounts return 404; missing credentials return 401.

## Verify

```bash
make verify       # Build test container; Ruff + all tests on real PostgreSQL
```

Import both files in [`postman/`](postman/) and put your private keys into a local Postman environment. Run the collection in order: it creates fresh scenario data and includes replay, conflicts, ownership, cash, positions and reconciliation. Never export an environment containing real keys into Git.

Validated locally on 2026-09-25: **110 tests passed**, Ruff check/format passed; all five scenarios also passed through the live streaming API. Earlier Postman validation: Postman/Newman **24 requests, 41 assertions, zero failures**. GitHub Actions is configured; its remote result is pending push. Tests use disposable database schemas and independent connections for concurrency. They do not reset your demo account.

[`artifacts/benchmark.json`](artifacts/benchmark.json) records a local hot-key replay measurement: 100 requests, concurrency 10, 215.91 requests/sec, p95 54.507 ms, zero errors, invariants checked. This small run is not evidence of production capacity or partner throughput.

Presentation artifacts: [panel](artifacts/panel.png), [60-second recording](artifacts/demo-60s.webm), [five-minute walkthrough](artifacts/walkthrough-5min.webm). Recordings show the running application with explanatory captions and no voice-over.

## Development

Optional PyCharm interpreter: `.venv/bin/python`. `make setup` installs the package and development dependencies. Run from the repository root:

```bash
make format       # Ruff format, then Ruff check --fix
make lint         # Check without modifying files
make verify-local # Full PostgreSQL tests using your local environment
make benchmark    # Live HTTP replay measurement; requires running stack
```

Ruff follows the reference service's E/F/I rule families, double quotes and four-space indentation, with the requested **79 columns** (the reference used 95). Ruff cannot automatically shorten every string. `requirements/base.txt` contains runtime dependencies; `dev.txt` adds tooling. `pyproject.toml` packages the application and configures pytest.

API source edits reload through the Compose mount. Rebuild to update worker/partner code or dependencies. For a schema change, use the local admin connection: `.venv/bin/alembic revision --autogenerate -m "Describe change"`, review the generated migration, then restart with `docker compose up --build`. The runtime API role deliberately cannot modify schema or accounting history. Partner migrations use `partner-alembic.ini` and its separate database. Never point that configuration at the local application database.

## Design and guarantees

```mermaid
flowchart LR
    Client -->|intention + idempotency key| Lab[Brokerage Lab]
    Operator -->|inspect and recover| Lab
    Lab -->|stable client_order_id| Partner[Execution partner]
    Partner -->|result and report| Lab
```

```mermaid
flowchart LR
    API[FastAPI + operator panel] --> DB[(Local PostgreSQL)]
    Worker -->|claim / acknowledge| DB
    Worker -->|HTTP outside DB transaction| Partner[Partner FastAPI]
    Partner --> PDB[(Partner PostgreSQL)]
    Reconciliation -->|HTTP report / lookup| Partner
    Reconciliation --> DB
```

```mermaid
sequenceDiagram
    participant W as Worker
    participant D as Local DB
    participant P as Partner
    W->>D: Claim outbox message; commit lease
    W->>P: Submit stable client_order_id
    P->>P: Commit deduplicated execution
    P--xW: Response lost
    W->>D: UNKNOWN; retain reservation
    W->>P: Lookup original client_order_id
    P-->>W: Existing execution
    W->>D: Atomic dedup + ledger + position + release + status
```

| Module | Responsibility |
|---|---|
| `domain.py`, `contracts.py`, `schemas.py` | Pydantic values, transitions and boundary validation |
| `services.py`, `repositories.py`, `unit_of_work.py` | Account locking, idempotency and explicit transaction ownership |
| `db.py`, `models.py`, `ledger.py`, `executions.py` | ORM, immutable balanced history, cash projection and execution deduplication |
| `worker.py`, `transport.py`, `partner.py` | Leased outbox delivery and durable remote simulator |
| `reconciliation.py`, `operations.py` | Cutoff-aware comparison, investigation and evidence-based recovery |
| `scenarios.py`, `panel.py` | Repeatable failure demonstrations and persisted observations |

<details><summary>Five architecture decisions</summary>

1. **Pydantic domain, separate SQLAlchemy persistence.** Domain operations return validated new state. Boundary schemas validate input; domain operations enforce business rules. This retains familiar models without making ORM rows the public contract.
2. **Explicit service transaction and small repositories.** One service owns commit; repositories flush only. A unit of work rolls back on exit without commit. This makes the multi-table order/reservation/idempotency/outbox boundary visible. It is a pragmatic separation, not a framework requirement; ordinary CRUD remains suitable for simple independent updates.
3. **At-least-once delivery with leased outbox.** Order and delivery intent commit together. Network calls happen outside local transactions. Lease tokens reject stale acknowledgements. Remote retries need stable identity, durable deduplication and lookup; a local DB cannot enforce remote exactly-once behavior.
4. **Append-only double-entry cash history.** Every journal balances EUR against a clearing bucket; units live in a separate register. Execution identity and all effects commit together. Corrections append linked opposite effects; originals remain unchanged. Constraints, triggers and restricted runtime grants protect history, not against database administrators.
5. **Evidence-driven reconciliation and explicit uncertainty.** Compare five discrepancy categories at the report cutoff. Missing local executions can be recovered through validated partner lookup and normal booking. A mismatch alone cannot repair cash. UNKNOWN and exhausted retries retain reservations; operator attention is required.

</details>

<details><summary>Postmortem: rejected orders incorrectly appeared missing</summary>

During implementation review, the local reconciliation snapshot contained only booked executions, while the partner report also included confirmed rejected orders. This mismatch in record selection would classify a known rejection as MISSING_LOCAL. No production system or real funds were involved.

The fix includes dated ACCEPTED/REJECTED acknowledgement evidence in the local snapshot and reconstructs state at the cutoff even if a fill arrived later. Recovery also rejects contradictory ACCEPTED/REJECTED responses carrying execution data. `test_confirmed_rejection_is_not_missing_execution` now submits a rejection through the partner HTTP path and asserts an empty discrepancy set. Pure comparison tests alone could not establish that the report inputs represented the same population.

</details>

## Boundaries

This is a development MVP. Local API keys and browser Basic authentication are not a production identity system. Failure controls run only in development mode. Retry exhaustion goes to a visible error queue without releasing cash. Recovery, high availability, retention policies, production credentials/TLS, real broker integration and settlement require further design.

The partner simulator demonstrates a contract; it does not establish that any real provider offers these guarantees. Existing pre-outbox demo orders are preserved rather than automatically sent. Use new panel runs for clean demonstrations. A demo reset refuses booked history; it is not an accounting eraser.
