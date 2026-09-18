"""GridWise LLM - FastAPI entrypoint.

Request flow for POST /optimize-energy:
    raw body -> JSON parse (400) -> schema checks (400 / 422)
             -> LLM interpreter -> guardrails            (app/interpreter.py, app/guardrails.py)
             -> exact LP optimizer                       (app/optimizer.py)
             -> self-replay validator                    (app/validator.py)
             -> JSON response (exact Problem Statement schema)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import optimizer
from .config import load_settings
from .guardrails import clean_number
from .interpreter import NoteCache, InterpretationError, interpret_notes
from .llm_client import LLMClient
from .schemas import RequestError, Scenario, parse_scenario
from .validator import check_interpretation_schema, replay_plan

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gridwise.api")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
settings = load_settings()
state: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    optimizer.warm_up()  # load scipy/HiGHS once, so the first request is fast
    state["llm"] = LLMClient(settings.providers)
    state["cache"] = NoteCache(settings.cache_size)
    state["solver_lock"] = asyncio.Semaphore(1)
    state["started"] = True
    if settings.providers:
        for p in settings.providers:  # never log the key itself
            log.info("LLM configured: %s (%s)", p.label, p.provider)
    else:
        log.warning("LLM not configured. Set LLM_PROVIDER, LLM_MODEL and LLM_API_KEY in .env or environment.")
    try:
        yield
    finally:
        state["started"] = False
        await state["llm"].aclose()


app = FastAPI(title="GridWise LLM", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- errors


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": message})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_request: Request, exc: StarletteHTTPException):
    return error_response(exc.status_code, "http_error", str(exc.detail))


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception):
    # Controlled 500: log the type only, never a stack trace or a secret in the response.
    log.error("unexpected error type: %s", type(exc).__name__)
    return error_response(500, "internal_error", "The server could not produce a validated plan.")


# ---------------------------------------------------------------- routes


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    if not state.get("started") or not settings.providers:
        return error_response(500, "not_ready", "Configure a language-model provider before judging.")
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"service": "GridWise LLM", "endpoints": ["GET /health", "POST /optimize-energy"]}


@app.post("/optimize-energy")
async def optimize_energy(request: Request):
    started = time.perf_counter()

    # 1) parse + validate the request (400 malformed / 422 impossible values)
    raw = await request.body()
    try:
        body = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return error_response(400, "malformed_json", "Request body must be valid JSON.")
    try:
        scenario = parse_scenario(body)
    except RequestError as exc:
        return error_response(exc.status, exc.code, exc.message)

    try:
        return await asyncio.wait_for(_run_pipeline(scenario, started), timeout=27.0)
    except InterpretationError:
        return error_response(500, "llm_unavailable", "Could not interpret every note safely. Check the LLM configuration, quota and availability.")
    except optimizer.InfeasiblePlanError:
        state["cache"].discard(scenario.notes, scenario.battery)
        return error_response(422, "infeasible_directives", "No feasible plan satisfies all interpreted directives. No constraints were dropped.")
    except (optimizer.OptimizerError, asyncio.TimeoutError):
        return error_response(500, "optimization_unavailable", "Could not complete a validated plan within the request budget.")
    except Exception as exc:
        log.error("Request failed: %s", type(exc).__name__)
        return error_response(500, "internal_error", "The server could not produce a validated plan.")


def _reject_constant(_value):
    raise ValueError("JSON numbers must be finite")


async def _run_pipeline(scenario: Scenario, started: float):
    interpretations, sources = await interpret_notes(
        scenario.notes, scenario.battery, state["llm"], state["cache"], settings)
    problems = check_interpretation_schema(len(scenario.notes), interpretations, scenario.battery.capacity)
    if problems:
        return error_response(500, "invalid_interpretation", "Model interpretation failed validation.")
    async with state["solver_lock"]:
        result = await run_in_threadpool(optimizer.optimize, scenario, interpretations)
    response = build_response(scenario, interpretations, result)
    # Validate ALL reported directives. Never check only a relaxed subset.
    problems = replay_plan(scenario, interpretations, response, tol=1e-6)
    if problems:
        log.error("Plan rejected by final replay: %d check(s)", len(problems))
        return error_response(500, "plan_validation_failed", "The computed schedule failed validation; no plan was returned.")
    elapsed = time.perf_counter() - started
    log.info("Validated plan: sources=%s latency=%.3fs", ",".join(sources), elapsed)
    return JSONResponse(content=response, headers={
        "X-Interpreter-Sources": ",".join(sources),
        "X-Plan-Status": result.status,
        "X-Self-Check": "pass",
    })


def build_response(scenario: Scenario, interpretations: list[dict], result: optimizer.OptimizationResult) -> dict:
    plan = result.plan
    total_grid = sum(row["grid_kwh"] for row in plan)
    total_cost = sum(row["grid_kwh"] * hour.tariff for row, hour in zip(plan, scenario.hours))
    peak = max(row["grid_kwh"] for row in plan)
    response = {
        "scenario_id": scenario.scenario_id,
        "directive_interpretation": interpretations,
        "hourly_plan": plan,
        "total_grid_kwh": clean_number(total_grid),
        "total_cost_bdt": clean_number(total_cost),
        "peak_grid_kwh": clean_number(peak),
        "plan_summary": "",
    }
    response["plan_summary"] = plan_summary(scenario, interpretations, result, response)
    return response


def _clock(hours: list[int]) -> str:
    """[18, 19, 20, 22] -> '18:00-21:00, 22:00-23:00' (end-exclusive, like the notes)."""
    spans, start, prev = [], None, None
    for h in sorted(hours):
        if start is None:
            start = prev = h
        elif h == prev + 1:
            prev = h
        else:
            spans.append((start, prev))
            start = prev = h
    if start is not None:
        spans.append((start, prev))
    return ", ".join(f"{a:02d}:00-{b + 1:02d}:00" for a, b in spans)


def _describe(entry: dict) -> str:
    adj = entry["structured_adjustment"]
    kind = entry["directive_type"]
    text = f"{kind} {_clock(adj['hours'])}"
    if kind == "solar_reduction":
        text += f" (solar x{adj['factor']})"
    elif kind == "minimum_battery_reserve":
        text += f" (>= {adj['minimum_energy_kwh']} kWh)"
    elif kind == "max_grid_window":
        text += f" (grid <= {adj['max_grid_kwh']} kWh)"
    return text


def plan_summary(scenario: Scenario, interpretations: list[dict], result, response: dict) -> str:
    applied = [_describe(d) for d in interpretations if d["applies"]]
    ignored = sum(1 for d in interpretations if not d["applies"])
    parts = []
    if applied:
        parts.append("Applied " + "; ".join(applied) + ".")
    if ignored:
        parts.append(f"{ignored} note(s) did not affect the schedule (no_op).")

    charge_hours = [r["hour"] for r in result.plan if r["battery_action"] == "charge"]
    discharge_hours = [r["hour"] for r in result.plan if r["battery_action"] == "discharge"]
    if charge_hours or discharge_hours:
        charged = clean_number(sum(r["battery_kwh"] for r in result.plan if r["battery_action"] == "charge"))
        parts.append(
            f"Battery charges {charged} kWh in hours ({_clock(charge_hours) or 'none'}) and "
            f"discharges in hours ({_clock(discharge_hours) or 'none'}), ending the day at "
            f"{clean_number(scenario.battery.initial)} kWh."
        )
    else:
        parts.append("Battery stays idle in this minimum-cost solution.")
    parts.append(
        f"Least-cost LP plan: {response['total_grid_kwh']} kWh from grid, "
        f"{response['total_cost_bdt']} BDT, peak {response['peak_grid_kwh']} kWh."
    )
    return " ".join(parts)
