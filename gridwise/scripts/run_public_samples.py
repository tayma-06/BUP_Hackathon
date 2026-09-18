"""Check a live API against organizer ground truth, or verify only the optimizer offline."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from app.main import build_response
from app.optimizer import optimize
from app.schemas import parse_scenario
from app.validator import replay_plan


def compare_entries(expected, actual):
    errors = []
    if not isinstance(actual, list) or len(actual) != len(expected):
        return ["Wrong number of interpretations"]
    for truth, got in zip(expected, actual):
        if not isinstance(got, dict):
            errors.append("Interpretation is not an object")
            continue
        for key in ("note_index", "applies", "directive_type"):
            if got.get(key) != truth[key]: errors.append(f"note {truth['note_index']}: incorrect {key}")
        e, a = truth["structured_adjustment"], got.get("structured_adjustment")
        if e is None:
            if a is not None: errors.append("no_op must have null adjustment")
            continue
        if not isinstance(a, dict) or set(a) != set(e):
            errors.append(f"note {truth['note_index']}: incorrect adjustment shape")
            continue
        for key, value in e.items():
            if key == "hours":
                if a[key] != value: errors.append(f"note {truth['note_index']}: incorrect hours")
            elif isinstance(a[key], bool) or not isinstance(a[key], (int, float)) or not math.isfinite(a[key]) or abs(a[key]-value) > 0.01:
                errors.append(f"note {truth['note_index']}: incorrect {key}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--offline", action="store_true", help="Use supplied interpretations; NOT a language-model test")
    parser.add_argument("--cases", type=Path, default=ROOT / "data/public_samples.json")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.repeat < 1: parser.error("--repeat must be at least 1")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    print("OFFLINE: supplied interpretations; no LLM inference." if args.offline else "LIVE: interpretation and plan checked against organizer ground truth.")
    records = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        if not args.offline:
            try:
                health = client.get(args.base_url.rstrip("/") + "/health")
                if health.status_code != 200 or health.json().get("status") != "ok":
                    print("Health failed. Check the service logs and LLM environment variables.")
                    return 1
            except (httpx.HTTPError, ValueError):
                print("Service not reachable. Start it with python run.py, or check --base-url.")
                return 1
        for run in range(args.repeat):
            for case in cases:
                started = time.perf_counter()
                record = {"case": case["id"], "run": run+1, "errors": []}
                try:
                    scenario = parse_scenario(case["input"])
                    truth = case["expected_output"]
                    if args.offline:
                        entries = truth["directive_interpretation"]
                        response = build_response(scenario, entries, optimize(scenario, entries))
                        record["source"] = "organizer_ground_truth_no_llm"
                    else:
                        result = client.post(args.base_url.rstrip("/") + "/optimize-energy", json=case["input"])
                        result.raise_for_status()
                        response = result.json()
                        record["source"] = result.headers.get("X-Interpreter-Sources", "unknown")
                        if not all(x.startswith(("llm:", "cache:llm")) for x in record["source"].split(",")):
                            record["errors"].append("No confirmed LLM provenance in response headers")
                    record["errors"] += compare_entries(truth["directive_interpretation"], response.get("directive_interpretation"))
                    # Always replay against ORGANIZER directives, never trust self-reported rules.
                    record["errors"] += replay_plan(scenario, truth["directive_interpretation"], response)
                    cost = response.get("total_cost_bdt")
                    record["cost_bdt"], record["reference_cost_bdt"] = cost, truth["total_cost_bdt"]
                    if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost > truth["total_cost_bdt"] + 0.01:
                        record["errors"].append("Cost exceeds reference optimum")
                except httpx.HTTPStatusError as exc:
                    record["errors"].append(f"HTTP {exc.response.status_code}; check server logs/configuration")
                except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                    record["errors"].append(f"Check failed ({type(exc).__name__})")
                record["seconds"] = round(time.perf_counter() - started, 4)
                record["passed"] = not record["errors"]
                records.append(record)
                print(f"{case['id']} run {run+1}: {'PASS' if record['passed'] else 'FAIL'} | cost={record.get('cost_bdt', '-')} BDT | {record['seconds']:.3f}s")
                for error in record["errors"]: print("  " + error)
    durations = sorted(r["seconds"] for r in records)
    passed = sum(r["passed"] for r in records)
    p95 = durations[math.ceil(0.95 * len(durations))-1]
    summary = {"mode": "offline_optimizer_only" if args.offline else "live_api", "passed": passed,
               "total": len(records), "p95_seconds_nearest_rank": p95, "results": records}
    print(f"{passed}/{len(records)} passed; sample p95={p95:.3f}s. Offline time is NOT live API latency." if args.offline else f"{passed}/{len(records)} passed; sample p95={p95:.3f}s.")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
