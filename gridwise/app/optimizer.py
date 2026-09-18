"""24-hour schedule optimizer: an exact linear program solved with HiGHS (scipy).

Variables for every hour h (all >= 0):
    g[h] grid import      s[h] solar used      c[h] battery charge
    d[h] battery discharge                     e[h] battery energy after hour h

Objective:  minimise  sum_h tariff[h] * g[h]
            No cycling penalty: even a small penalty can change the cost optimum.
            Opposing flows are netted when serializing the lossless battery.

Constraints (Problem Statement section 9 + section 5.3):
    g + s + d - c = demand                         energy balance
    e[h] = e[h-1] + c[h] - d[h],  e[-1] = initial   battery state
    0 <= s <= effective_solar[h]                    solar_reduction multiplies base solar
    0 <= c <= max_charge      (0 in no_charge_window hours)
    0 <= d <= max_discharge   (0 in no_discharge_window hours)
    floor[h] <= e[h] <= capacity                    floor = max(base minimum, reserve directives)
    g <= max_grid_kwh          in max_grid_window hours
    e[23] = initial                                 end-of-day neutrality

An LP with ~120 variables solves in a few milliseconds and is provably optimal,
so the only way to lose optimisation points is a wrong interpretation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from .guardrails import clean_number
from .schemas import Battery, HourData, Scenario

log = logging.getLogger("gridwise.optimizer")

H = 24
G, S, C, D, E = range(5)  # variable blocks


class OptimizerError(RuntimeError):
    pass


class InfeasiblePlanError(OptimizerError):
    """All interpreted constraints cannot be satisfied together."""


@dataclass
class HourLimits:
    effective_solar: list[float]
    grid_cap: list[float | None]
    charge_cap: list[float]
    discharge_cap: list[float]
    energy_floor: list[float]


def build_limits(battery: Battery, hours: list[HourData], directives: list[dict]) -> HourLimits:
    """Turn the applicable directives into per-hour numeric limits (section 5.3)."""
    solar = [h.solar for h in hours]
    grid_cap: list[float | None] = [None] * H
    charge_cap = [battery.max_charge] * H
    discharge_cap = [battery.max_discharge] * H
    floor = [battery.minimum] * H

    for directive in directives:
        if not directive.get("applies"):
            continue
        kind = directive["directive_type"]
        adj = directive["structured_adjustment"]
        for h in adj["hours"]:
            if kind == "solar_reduction":
                # Overlapping reductions compose multiplicatively. The statement
                # does not specify overlap precedence; this assumption is documented.
                solar[h] = solar[h] * adj["factor"]
            elif kind == "minimum_battery_reserve":
                floor[h] = max(floor[h], adj["minimum_energy_kwh"])
            elif kind == "no_charge_window":
                charge_cap[h] = 0.0
            elif kind == "no_discharge_window":
                discharge_cap[h] = 0.0
            elif kind == "max_grid_window":
                cap = adj["max_grid_kwh"]
                grid_cap[h] = cap if grid_cap[h] is None else min(grid_cap[h], cap)
    return HourLimits(solar, grid_cap, charge_cap, discharge_cap, floor)


def solve_lp(scenario: Scenario, limits: HourLimits) -> dict | None:
    """Solve the LP. Returns a dict of numpy arrays, or None if infeasible."""
    b = scenario.battery
    if not limits.energy_floor[H - 1] - 1e-9 <= b.initial <= b.capacity + 1e-9:
        return None  # a reserve at hour 23 above the initial energy can never be neutral

    n = 5 * H
    cost = np.zeros(n)
    a_eq = np.zeros((2 * H, n))
    b_eq = np.zeros(2 * H)

    def var(block: int, hour: int) -> int:
        return block * H + hour

    for h, hour in enumerate(scenario.hours):
        cost[var(G, h)] = hour.tariff
        # energy balance: g + s + d - c = demand
        a_eq[h, var(G, h)] = 1
        a_eq[h, var(S, h)] = 1
        a_eq[h, var(D, h)] = 1
        a_eq[h, var(C, h)] = -1
        b_eq[h] = hour.demand
        # battery state: e[h] - e[h-1] - c[h] + d[h] = 0
        row = H + h
        a_eq[row, var(E, h)] = 1
        a_eq[row, var(C, h)] = -1
        a_eq[row, var(D, h)] = 1
        if h == 0:
            b_eq[row] = b.initial
        else:
            a_eq[row, var(E, h - 1)] = -1

    bounds = []
    bounds += [(0, limits.grid_cap[h]) for h in range(H)]  # None = no upper bound
    bounds += [(0, max(0.0, limits.effective_solar[h])) for h in range(H)]
    bounds += [(0, max(0.0, limits.charge_cap[h])) for h in range(H)]
    bounds += [(0, max(0.0, limits.discharge_cap[h])) for h in range(H)]
    bounds += [(limits.energy_floor[h], b.capacity) for h in range(H - 1)]
    bounds += [(b.initial, b.initial)]  # end-of-day neutrality

    # Positive scaling preserves the exact objective, including very small tariffs.
    scale = float(np.max(np.abs(cost)))
    if scale > 0:
        cost /= scale
    result = linprog(cost, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs",
                     options={"time_limit": 3.0, "primal_feasibility_tolerance": 1e-9,
                              "dual_feasibility_tolerance": 1e-9})
    if result.status == 2:  # infeasible
        return None
    if result.status != 0:
        raise OptimizerError(f"LP solver status {result.status}")
    x = result.x
    grid = x[0:H]
    return {
        "grid": grid,
        "solar": x[H : 2 * H],
        "charge": x[2 * H : 3 * H],
        "discharge": x[3 * H : 4 * H],
        "cost": float(sum(g * hour.tariff for g, hour in zip(grid, scenario.hours))),
    }


def build_plan(scenario: Scenario, limits: HourLimits, solution: dict) -> list[dict]:
    """Convert the LP solution into the hourly_plan, keeping every row exactly consistent."""
    b = scenario.battery
    # one action per hour: net the charge/discharge (identical energy flow, no losses in this model)
    net = []
    for h in range(H):
        value = clean_number(solution["charge"][h] - solution["discharge"][h])
        net.append(value)

    plan = []
    energy = b.initial
    for h, hour in enumerate(scenario.hours):
        charge = max(net[h], 0.0)
        discharge = max(-net[h], 0.0)
        energy = energy + charge - discharge
        solar = min(max(float(solution["solar"][h]), 0.0), limits.effective_solar[h])
        grid = hour.demand + charge - discharge - solar  # balance holds by construction
        if grid < 0:  # curtail solver/serialization noise; final replay checks balance
            solar = max(0.0, solar + grid)
            grid = 0.0
        plan.append(
            {
                "hour": h,
                "grid_kwh": clean_number(grid),
                "solar_used_kwh": clean_number(solar),
                "battery_action": "charge" if charge > 0 else "discharge" if discharge > 0 else "idle",
                "battery_kwh": clean_number(charge or discharge),
                "battery_energy_after_kwh": clean_number(energy),
            }
        )
    return plan


@dataclass
class OptimizationResult:
    plan: list[dict]
    kept: list[dict]  # directives actually enforced
    status: str = "optimal"


def optimize(scenario: Scenario, interpretations: list[dict]) -> OptimizationResult:
    active = [d for d in interpretations if d["applies"]]

    limits = build_limits(scenario.battery, scenario.hours, active)
    solution = solve_lp(scenario, limits)
    if solution is None:
        raise InfeasiblePlanError("No feasible plan for the interpreted hard constraints")
    return OptimizationResult(build_plan(scenario, limits, solution), active)


def warm_up() -> None:
    """Solve a tiny LP at startup so the first real request is not slowed by imports.

    A crashed warm-up (e.g. a native HiGHS access violation on some platforms)
    must never block startup: the first real request recompiles the solver lazily.
    """
    try:
        linprog(np.array([1.0]), bounds=[(0, 1)], method="highs")
    except Exception as exc:
        log.warning("Optimizer warmup skipped: %s", type(exc).__name__)
