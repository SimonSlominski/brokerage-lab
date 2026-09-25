"""Small server-rendered operator panel with evidence from durable records."""

import json
from html import escape
from pathlib import Path
from string import Template

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import OrderRow
from .models import ReconciliationBreak, ScenarioRun
from .operations import metrics
from .services import timeline


def render_panel(engine) -> str:
    def safe(value):
        return escape(str(value))

    with Session(engine) as db:
        runs = db.scalars(
            select(ScenarioRun)
            .order_by(ScenarioRun.created_at.desc())
            .limit(20)
        ).all()
        orders = db.scalars(
            select(OrderRow).order_by(OrderRow.id).limit(100)
        ).all()
        cases = db.scalars(select(ReconciliationBreak).limit(100)).all()
        counters = metrics(db)
        run_html = "".join(
            f"<details><summary>{safe(row.scenario)} · {safe(row.status)} · "
            f"{safe(row.id)}</summary><p>{safe(row.created_at)} "
            f"· seed {row.seed}</p>"
            f"<pre>{safe(json.dumps(row.evidence, indent=2))}</pre>"
            f"<p>{safe(row.error or '')}</p></details>"
            for row in runs
        )
        order_html = "".join(
            f"<details><summary>{safe(row.id)} · "
            f"{safe(row.business_status)} / "
            f"{safe(row.communication_status)}</summary>"
            f"<p>Account: {safe(row.account_id)}"
            f" · EUR {safe(row.reservation_amount)} original reservation</p>"
            f"<pre>{safe(json.dumps(timeline(db, row.id), indent=2))}</pre>"
            "</details>"
            for row in orders
        )
        case_html = "".join(
            f"<details><summary>{safe(row.category)} · "
            f"{safe(row.status)}</summary>"
            f"<p>Case {safe(row.id)} · Run {safe(row.run_id)}</p>"
            f"<pre>{safe(json.dumps(row.evidence, indent=2))}</pre></details>"
            for row in cases
        )
    buttons = "".join(
        f'<button data-scenario="{name}" '
        f'onclick="run(this.dataset.scenario)">{label}</button>'
        for name, label in [
            ("lost_response", "Lost response"),
            ("retry_storm", "20 retries"),
            ("worker_crash", "Worker crash"),
            ("duplicate_execution", "10 executions"),
            ("amount_mismatch", "975 / 950 mismatch"),
        ]
    )

    template = Template(
        (Path(__file__).parent / "templates/lab.html").read_text()
    )
    return template.substitute(
        buttons=buttons,
        counters=safe(json.dumps(counters, indent=2)),
        runs=run_html or "<p>No runs yet.</p>",
        orders=order_html or "<p>No orders.</p>",
        cases=case_html or "<p>No cases.</p>",
    )
