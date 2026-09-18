"""Data model / validation layer tests.

The service intentionally does not use pydantic models: app/schemas.py defines
dataclasses plus a manual validator (parse_scenario) that maps structural
errors to 400 and physically impossible values to 422. These tests pin that
contract so the layer behaves like a typed model layer.
"""
import pytest

from app.schemas import Battery, HourData, RequestError, Scenario, parse_scenario


@pytest.fixture
def valid_hour_list():
    return [{"hour": h, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 6} for h in range(24)]


def test_valid_full_request_is_accepted(body_builder):
    scenario = parse_scenario(body_builder())
    assert isinstance(scenario, Scenario)
    assert scenario.scenario_id == "TEST-001"
    assert len(scenario.notes) == 1
    assert len(scenario.hours) == 24
    assert [h.hour for h in scenario.hours] == list(range(24))
    battery = scenario.battery
    assert (battery.capacity, battery.initial, battery.minimum) == (220, 110, 40)


def test_valid_battery_values_accepted(body_builder, battery_builder):
    body = body_builder(battery=battery_builder(capacity_kwh=220, initial_energy_kwh=100))
    scenario = parse_scenario(body)
    assert scenario.battery.capacity == 220
    assert scenario.battery.initial == 100


def test_battery_and_hour_data_dataclasses():
    hour = HourData(3, 10.0, 2.0, 6.0)
    assert (hour.hour, hour.demand, hour.solar, hour.tariff) == (3, 10.0, 2.0, 6.0)
    battery = Battery(220, 100, 40, 50, 50)
    assert (battery.capacity, battery.initial, battery.minimum,
            battery.max_charge, battery.max_discharge) == (220, 100, 40, 50, 50)


@pytest.mark.parametrize("capacity", [-20, -0.01])
def test_negative_battery_capacity_rejected(body_builder, battery_builder, capacity):
    body = body_builder(battery=battery_builder(capacity_kwh=capacity))
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 422


@pytest.mark.parametrize("initial", [-0.1, 39.9, 220.1])
def test_initial_energy_outside_bounds_rejected(body_builder, battery_builder, initial):
    body = body_builder(battery=battery_builder(initial_energy_kwh=initial))
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 422


@pytest.mark.parametrize("hour", [-1, 24, 12.5, True, "3", None])
def test_invalid_hour_index_rejected(body_builder, hour):
    body = body_builder()
    body["hours"][0]["hour"] = hour
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 400


def test_hour_indices_0_and_23_are_valid(body_builder):
    body = body_builder()
    body["hours"] = [{"hour": h, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 6} for h in range(24)]
    scenario = parse_scenario(body)
    assert scenario.hours[0].hour == 0
    assert scenario.hours[23].hour == 23


def test_negative_demand_rejected(body_builder):
    body = body_builder()
    body["hours"][5]["demand_kwh"] = -1
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 422


def test_negative_tariff_rejected(body_builder):
    body = body_builder()
    body["hours"][5]["tariff_bdt_per_kwh"] = -1
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 422


def test_boolean_rejected_as_number(body_builder, valid_hour_list):
    body = body_builder(hours=valid_hour_list)
    body["hours"][0]["demand_kwh"] = True
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 400