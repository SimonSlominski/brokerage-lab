"""HTTP-only partner adapter; no partner database imports or credentials."""

import httpx

from .contracts import PartnerOrderCreate, PartnerReport, PartnerResult


class PartnerTransport:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 0.3):
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"X-Partner-Key": api_key},
        )

    def submit(self, obj_in: PartnerOrderCreate) -> PartnerResult:
        response = self.client.post(
            "/orders", json=obj_in.model_dump(mode="json")
        )
        response.raise_for_status()
        return PartnerResult.model_validate(response.json())

    def lookup(self, order_id: str) -> PartnerResult | None:
        response = self.client.get(f"/orders/{order_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return PartnerResult.model_validate(response.json())

    def report(self, *, run_id: str | None = None) -> PartnerReport:
        response = self.client.get(
            "/reports", params={"run_id": run_id} if run_id else {}
        )
        response.raise_for_status()
        return PartnerReport.model_validate(response.json())

    def close(self) -> None:
        self.client.close()

    def store_report_fixture(self, report: PartnerReport) -> PartnerReport:
        response = self.client.post(
            "/demo/reports", json=report.model_dump(mode="json")
        )
        response.raise_for_status()
        report_id = response.json()["report_id"]
        stored = self.client.get(f"/reports/{report_id}")
        stored.raise_for_status()
        return PartnerReport.model_validate(stored.json())
