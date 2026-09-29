"""Measure HTTP replay and verify cash and instrument effects."""

import argparse
import json
import os
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from .auth import DemoAuthSettings
from .config import Settings
from .contracts import utc_now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.requests <= 10000 or not 1 <= args.concurrency <= 100:
        parser.error("Choose 1..10000 requests and 1..100 concurrent workers")
    settings = Settings()
    with httpx.Client(base_url=args.base_url, timeout=40) as client:
        response = client.post(
            "/lab/runs",
            json={"scenario": "retry_storm", "seed": 42},
            headers={
                "X-Operator-Key": settings.operator_api_key.get_secret_value()
            },
        )
        response.raise_for_status()
        run = response.json()
        if run["status"] != "PASSED":
            raise RuntimeError("Benchmark setup scenario failed")
        account = run["evidence"]["final"]["account_id"]
        expected_id = run["evidence"]["final"]["orders"][0]["id"]
        headers = {
            "X-API-Key": DemoAuthSettings().demo_api_key.get_secret_value(),
            "Idempotency-Key": run["run_id"] + ":intent",
        }

        def replay(_):
            started = time.perf_counter()
            result = client.post(
                f"/accounts/{account}/orders",
                json={"quantity": 8},
                headers=headers,
            )
            valid = (
                result.status_code == 201
                and result.json().get("order_id") == expected_id
            )
            return (time.perf_counter() - started) * 1000, valid

        for index in range(20):
            _, valid = replay(index)
            if not valid:
                raise RuntimeError("Warmup replay failed")
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(replay, range(args.requests)))
        elapsed = time.perf_counter() - started
        cash = client.get(f"/accounts/{account}/cash", headers=headers).json()
        positions = client.get(
            f"/accounts/{account}/positions", headers=headers
        ).json()
        verified = (
            cash["posted_cash"] == "200.00"
            and cash["active_reservations"] == "0.00"
            and positions == [{"instrument": "SYNTH-100", "quantity": 8}]
        )
        latencies = sorted(latency for latency, _ in results)

        def percentile(value):
            index = min(
                len(latencies) - 1, max(0, int(len(latencies) * value) - 1)
            )
            return round(latencies[index], 3)

        report = dict(
            measured_at=utc_now().isoformat(),
            workload="Repeated HTTP POST of one committed client/key",
            dataset="One funded account, one filled order, eight units",
            host=dict(
                system=platform.system(),
                machine=platform.machine(),
                logical_cpus=os.cpu_count(),
            ),
            python=platform.python_version(),
            postgres="17 (Compose)",
            concurrency=args.concurrency,
            warmup_requests=20,
            measured_requests=args.requests,
            duration_seconds=round(elapsed, 4),
            requests_per_second=round(args.requests / elapsed, 2),
            p50_ms=round(statistics.median(latencies), 3),
            p95_ms=percentile(0.95),
            p99_ms=percentile(0.99),
            errors=sum(not valid for _, valid in results),
            invariants_verified=verified,
            run_id=run["run_id"],
            limitations="Local hot-key replay only; not broker throughput",
        )
        print(json.dumps(report, indent=2))
        if not verified or report["errors"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
