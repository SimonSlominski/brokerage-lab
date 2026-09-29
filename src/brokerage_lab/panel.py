"""Operator laboratory: persisted runs plus an evidence-backed walkthrough."""

import json
from pathlib import Path
from string import Template

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ScenarioRun


def render_panel(engine) -> str:
    with Session(engine) as db:
        runs = db.scalars(
            select(ScenarioRun)
            .order_by(ScenarioRun.created_at.desc())
            .limit(20)
        ).all()
        data = [
            dict(
                run_id=row.id,
                scenario=row.scenario,
                status=row.status,
                evidence=row.evidence,
                error=row.error,
                created_at=row.created_at.isoformat(),
            )
            for row in runs
        ]
    # JSON inside a script element must not be able to close that element.
    encoded = json.dumps(data).replace("<", "\\u003c")
    template = Template(
        (Path(__file__).parent / "templates/lab.html").read_text()
    )
    return template.substitute(runs_json=encoded)
