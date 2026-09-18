"""Optimizer correctness tests (app.optimizer).

The LP is exact (HiGHS), so the assertions pin the physical optimum: solar is
used first, the battery charges in cheap hours and discharges in expensive
hours, and infeasible constraint sets must be rejected, never dropped.
"""
import pytest

from app import main
from app.optimizer import InfeasiblePlanError, build_limits, optimize
from app.schemas import Battery, HourData, Scenario, parse_scenario
from app.validator import replay_plan


def noop_entry():
    return {"note_index": 0, "applies": False, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "None."}


def make_scenario(demand, solar, tariff, battery):
    hours = [HourData(h, demand(h), solar(h), tariff(h)) for h in range(24)]
    return Scenario("T", ["note"], hours, battery)


def reserve_entry(kwh=60.0, hours=(23,)):
    return {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": list(hours), "minimum_energy_kwh": kwh},
            "explanation": "None."}


def grid_cap_entry(cap=0.0):
    return {"note_index": 0, "applies": True, "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": list(range(24)), "max_grid_kwh": cap},
            "explanation": "None."}


def test_basic_optimization_returns_complete_schedule(body_builder):
    scenario = parse_scenario(body_builder())
    result = optimize(scenario, [noop_entry()])

    assert len(result.plan) == 24
    assert [row["hour"] for row in result.plan] == list(range(24))
    for row in result.plan:
        assert row["grid_kwh"] >= 0
        assert row["battery_action"] in ("charge", "discharge", "idle")
        assert row["battery_kwh"] >= 0
        assert row["battery_energy_after_kwh"] >= 0

    response = main.build_response(scenario, [noop_entry()], result)
    assert replay_plan(scenario, [noop_entry()], response, tol=1e-6) == []
    total_grid = sum(row["grid_kwh"] for row in result.plan)
    expected_cost = sum(row["grid_kwh"] * hour.tariff for row, hour in zip(result.plan, scenario.hours))
    assert response["total_cost_bdt"] == pytest.approx(expected_cost, abs=1e-6)
    assert total_grid > 0


def test_solar_is_used_to_cover_demand_when_available():
    scenario = make_scenario(
        demand=lambda h: 50.0,
        solar=lambda h: 30.0 if 10 <= h <= 13 else 0.0,
        tariff=lambda h: 1.0,
        battery=Battery(0, 0, 0, 0, 0),
    )
    result = optimize(scenario, [noop_entry()])
    for row in result.plan:
        if 10 <= row["hour"] <= 13:
            assert row["solar_used_kwh"] == pytest.approx(30.0)  # all free solar consumed
    assert sum(row["grid_kwh"] for row in result.plan) == pytest.approx(24 * 50 - 4 * 30)


def test_battery_discharges_during_expensive_hour():
    scenario = make_scenario(
        demand=lambda h: 30.0,
        solar=lambda h: 0.0,
        tariff=lambda h: 100.0 if h == 10 else 1.0,
        battery=Battery(100, 50, 0, 20, 20),
    )
    result = optimize(scenario, [noop_entry()])
    row10 = result.plan[10]
    assert row10["battery_action"] == "discharge"
    assert row10["battery_kwh"] > 0
    assert row10["grid_kwh"] < 30.0  # expensive import avoided
    assert any(row["battery_action"] == "charge" for row in result.plan)


def test_battery_charges_during_cheap_hours():
    scenario = make_scenario(
        demand=lambda h: 10.0,
        solar=lambda h: 0.0,
        tariff=lambda h: 1.0 if h < 6 else 2.0,
        battery=Battery(100, 50, 0, 20, 20),
    )
    result = optimize(scenario, [noop_entry()])
    cheap_charges = [row for row in result.plan[:6] if row["battery_action"] == "charge"]
    expensive_discharges = [row for row in result.plan[6:] if row["battery_action"] == "discharge"]
    assert cheap_charges
    assert expensive_discharges


def test_flat_tariffs_reach_the_cost_lower_bound():
    # Flat tariffs make the objective degenerate (many equal-cost optima), so the
    # invariant is that the plan reaches the theoretical minimum: with a net-zero
    # battery the grid must cover exactly the summed demand, never more.
    scenario = make_scenario(
        demand=lambda h: 10.0,
        solar=lambda h: 0.0,
        tariff=lambda h: 5.0,
        battery=Battery(100, 50, 0, 20, 20),
    )
    result = optimize(scenario, [noop_entry()])
    total_grid = sum(row["grid_kwh"] for row in result.plan)
    assert total_grid == pytest.approx(24 * 10.0)  # battery net cycle is zero
    assert result.plan[0]["grid_kwh"] >= 0 and result.plan[23]["grid_kwh"] >= 0
    response = main.build_response(scenario, [noop_entry()], result)
    assert replay_plan(scenario, [noop_entry()], response, tol=1e-6) == []
    assert response["total_cost_bdt"] == pytest.approx(24 * 10.0 * 5.0, abs=1e-6)


def test_solar_reduction_is_applied_multiplicatively():
    battery = Battery(220, 110, 40, 50, 50)
    hours = [HourData(h, 100.0, 40.0, 6.0) for h in range(24)]
    entries = [{
        "note_index": 0, "applies": True, "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [12, 13], "factor": 0.25}, "explanation": "None.",
    }]
    scenario = Scenario("T", ["solar"], hours, battery)
    result = optimize(scenario, entries)
    for row in result.plan:
        if row["hour"] in (12, 13):
            assert row["solar_used_kwh"] == pytest.approx(10.0)  # 40 * 0.25
        else:
            assert row["solar_used_kwh"] == pytest.approx(40.0)


def test_infeasible_reserve_at_end_of_day_raises():
    scenario = make_scenario(
        demand=lambda h: 10.0,
        solar=lambda h: 0.0,
        tariff=lambda h: 1.0,
        battery=Battery(100, 50, 0, 20, 20),
    )
    with pytest.raises(InfeasiblePlanError):
        optimize(scenario, [reserve_entry(kwh=60.0, hours=[23])])


def test_infeasible_grid_cap_raises():
    scenario = make_scenario(
        demand=lambda h: 50.0,
        solar=lambda h: 0.0,
        tariff=lambda h: 1.0,
        battery=Battery(0, 0, 0, 0, 0),
    )
    with pytest.raises(InfeasiblePlanError):
        optimize(scenario, [grid_cap_entry(0.0)])


def test_build_limits_applies_every_directive_kind():
    battery = Battery(220, 110, 40, 50, 50)
    hours = [HourData(h, 100.0, 40.0, 6.0) for h in range(24)]
    entries = [
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [12, 13], "factor": 0.25}, "explanation": ""},
        {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14]}, "explanation": ""},
        {"note_index": 2, "applies": True, "directive_type": "no_discharge_window",
         "structured_adjustment": {"hours": [15]}, "explanation": ""},
        {"note_index": 3, "applies": True, "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [16], "minimum_energy_kwh": 80.0}, "explanation": ""},
        {"note_index": 4, "applies": True, "directive_type": "max_grid_window",
         "structured_adjustment": {"hours": [17], "max_grid_kwh": 90.0}, "explanation": ""},
    ]
    limits = build_limits(battery, hours, entries)
    assert limits.effective_solar[12] == pytest.approx(10.0)
    assert limits.charge_cap[14] == 0.0
    assert limits.discharge_cap[15] == 0.0
    assert limits.energy_floor[16] == pytest.approx(80.0)
    assert limits.grid_cap[17] == pytest.approx(90.0)
    assert limits.grid_cap[18] is None