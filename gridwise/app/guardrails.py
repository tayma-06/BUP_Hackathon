"""Deterministic guardrails between the LLM and the optimizer.

The LLM output is treated as UNTRUSTED data. `validate_entry` converts one raw
LLM entry into the exact directive_interpretation shape from the Problem
Statement, or raises GuardrailError with a human-readable reason (the reason is
sent back to the LLM on a retry).

What is enforced here:
  * directive_type must be one of the 6 supported types (small alias table for
    harmless spelling variants such as "no-op" -> "no_op")
  * applies is DERIVED from the type: no_op -> false + null adjustment,
    everything else -> true (the LLM cannot get this wrong)
  * time windows [start, end) are expanded to unique integer hours 0-23,
    sorted ascending (windows that cross midnight are supported)
  * solar factor must be in [0, 1]
  * reserve must be finite, >= 0 and <= battery capacity
    (a "% of capacity" reserve is converted to kWh HERE, not by the LLM)
  * grid cap must be finite and >= 0
  * the adjustment object has exactly the keys required for its type
"""

from __future__ import annotations

import math
import re

from .schemas import Battery

ALLOWED_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)

_ALIASES = {
    "noop": "no_op",
    "none": "no_op",
    "irrelevant": "no_op",
    "not_applicable": "no_op",
    "min_battery_reserve": "minimum_battery_reserve",
    "minimum_reserve": "minimum_battery_reserve",
    "battery_reserve": "minimum_battery_reserve",
    "no_charge": "no_charge_window",
    "no_discharge": "no_discharge_window",
    "max_grid": "max_grid_window",
    "grid_cap": "max_grid_window",
    "max_grid_import": "max_grid_window",
    "solar_reduce": "solar_reduction",
}

DEFAULT_EXPLANATIONS = {
    "solar_reduction": "Usable solar is reduced during the stated hours.",
    "minimum_battery_reserve": "A minimum battery reserve is required during the stated hours.",
    "no_charge_window": "Battery charging is unavailable during the stated hours.",
    "no_discharge_window": "Battery discharging is unavailable during the stated hours.",
    "max_grid_window": "Grid import is capped during the stated hours.",
    "no_op": "This note does not affect today's 24-hour energy schedule.",
}

MAX_EXPLANATION_CHARS = 240


class GuardrailError(ValueError):
    """Raised when an LLM entry is not safe to apply."""


def clean_number(value: float) -> float | int:
    """Keep fractional directives precise and serialize whole numbers as ints."""
    value = round(float(value), 12)
    if not math.isfinite(value):
        raise GuardrailError("result must be finite")
    if value == 0:
        return 0  # also turns -0.0 into 0
    if value.is_integer():
        return int(value)
    return value


def normalize_directive_type(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuardrailError("directive_type is missing")
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    key = _ALIASES.get(key, key)
    if key not in ALLOWED_TYPES:
        raise GuardrailError("unsupported directive_type; use one of " + ", ".join(ALLOWED_TYPES))
    return key


def _as_number(value: object, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise GuardrailError(f"{name} must be a number")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise GuardrailError(f"{name} must be a number") from None
    if not isinstance(value, (int, float)):
        raise GuardrailError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise GuardrailError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise GuardrailError(f"{name} must be a finite number")
    return number


def _as_hour(value: object, low: int, high: int, name: str) -> int:
    number = _as_number(value, name)
    if not number.is_integer():
        raise GuardrailError(f"{name} must be a whole hour, got {number}")
    hour = int(number)
    if not low <= hour <= high:
        raise GuardrailError(f"{name} must be between {low} and {high}, got {hour}")
    return hour


def _hours_from_windows(windows: object) -> list[int]:
    if isinstance(windows, dict):
        windows = [windows]
    if not isinstance(windows, list):
        raise GuardrailError("windows must be a list of [start_hour, end_hour] pairs")
    # tolerate a single bare pair like [13, 15]
    if len(windows) == 2 and all(isinstance(x, (int, float, str)) and not isinstance(x, bool) for x in windows):
        windows = [windows]

    hours: set[int] = set()
    for window in windows:
        if isinstance(window, dict):
            start = window.get("start_hour", window.get("start"))
            end = window.get("end_hour", window.get("end"))
        elif isinstance(window, (list, tuple)) and len(window) == 2:
            start, end = window
        else:
            raise GuardrailError("each window must be [start_hour, end_hour]")
        start = _as_hour(start, 0, 23, "window start")
        end = _as_hour(end, 0, 24, "window end")
        if start == end:
            raise GuardrailError("window start and end are equal (empty window); use end = start + 1 for one hour")
        if start < end:
            hours.update(range(start, end))
        else:  # crosses midnight, e.g. [22, 2] -> 22, 23, 0, 1
            hours.update(range(start, 24))
            hours.update(range(0, end))
    return sorted(hours)


def _hours_from_list(values: object) -> list[int]:
    if not isinstance(values, list):
        raise GuardrailError("hours must be a list of integers 0-23")
    return sorted({_as_hour(v, 0, 23, "hour") for v in values})


def extract_hours(raw: dict) -> list[int]:
    windows = raw.get("windows")
    listed = raw.get("hours")
    if windows not in (None, []):
        hours = _hours_from_windows(windows)  # windows win: they are end-exclusive by contract
        if listed not in (None, []) and hours != _hours_from_list(listed):
            raise GuardrailError("windows and hours disagree")
    elif listed not in (None, []):
        hours = _hours_from_list(listed)
    else:
        raise GuardrailError("no time window given (windows or hours)")
    if not hours:
        raise GuardrailError("time window produced no hours")
    return hours


def _reserve_kwh(raw: dict, battery: Battery) -> float:
    percent = raw.get("minimum_energy_percent")
    kwh = raw.get("minimum_energy_kwh")
    if percent is not None:
        pct = _as_number(percent, "minimum_energy_percent")
        if not 0 <= pct <= 100:
            raise GuardrailError("minimum_energy_percent must be between 0 and 100")
        value = battery.capacity * pct / 100.0
        if kwh is not None and abs(_as_number(kwh, "minimum_energy_kwh") - value) > 1e-8:
            raise GuardrailError("minimum_energy_percent and minimum_energy_kwh disagree")
    elif kwh is not None:
        value = _as_number(kwh, "minimum_energy_kwh")
    else:
        raise GuardrailError("minimum_battery_reserve needs minimum_energy_kwh or minimum_energy_percent")
    if value < 0:
        raise GuardrailError("reserve must be non-negative")
    if value > battery.capacity + 1e-9:
        raise GuardrailError(f"reserve {value:g} kWh exceeds battery capacity {battery.capacity:g} kWh")
    return min(value, battery.capacity)


def _explanation(raw: dict, dtype: str) -> str:
    text = raw.get("explanation")
    if not isinstance(text, str) or not text.strip():
        return DEFAULT_EXPLANATIONS[dtype]
    return " ".join(text.split())[:MAX_EXPLANATION_CHARS]


def validate_entry(raw: object, battery: Battery) -> dict:
    """Return {applies, directive_type, structured_adjustment, explanation} or raise GuardrailError."""
    if not isinstance(raw, dict):
        raise GuardrailError("entry must be a JSON object")
    allowed = {"note_index", "directive_type", "applies", "structured_adjustment",
               "windows", "hours", "factor", "minimum_energy_kwh",
               "minimum_energy_percent", "max_grid_kwh", "explanation"}
    if set(raw) - allowed:
        raise GuardrailError("entry includes unsupported fields")
    # If the model mimicked the final response shape, read fields from the nested object too.
    nested = raw.get("structured_adjustment")
    if nested is not None and not isinstance(nested, dict):
        raise GuardrailError("structured_adjustment must be an object or null")
    if isinstance(nested, dict):
        nested_allowed = {"hours", "windows", "factor", "minimum_energy_kwh",
                          "minimum_energy_percent", "max_grid_kwh"}
        if set(nested) - nested_allowed:
            raise GuardrailError("structured_adjustment includes unsupported fields")
        if any(k in raw and raw[k] is not None and raw[k] != v for k, v in nested.items()):
            raise GuardrailError("nested and top-level adjustment values disagree")
        raw = {**nested, **{k: v for k, v in raw.items() if v is not None}}

    dtype = normalize_directive_type(raw.get("directive_type"))
    if "applies" in raw and raw["applies"] is not (dtype != "no_op"):
        raise GuardrailError("applies must be false only for no_op, true otherwise")
    numeric_fields = {"factor", "minimum_energy_kwh", "minimum_energy_percent", "max_grid_kwh"}
    permitted = {"solar_reduction": {"factor"},
                 "minimum_battery_reserve": {"minimum_energy_kwh", "minimum_energy_percent"},
                 "max_grid_window": {"max_grid_kwh"}}.get(dtype, set())
    if any(raw.get(k) is not None for k in numeric_fields - permitted):
        raise GuardrailError("numeric adjustment does not match directive_type")
    if dtype == "no_op":
        if nested is not None or raw.get("hours") not in (None, []) or raw.get("windows") not in (None, []):
            raise GuardrailError("no_op must not contain an adjustment or time window")
        return {
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": _explanation(raw, dtype),
        }

    hours = extract_hours(raw)
    if dtype == "solar_reduction":
        factor = _as_number(raw.get("factor"), "factor")
        if not 0 <= factor <= 1:
            raise GuardrailError("factor must be the REMAINING solar fraction between 0 and 1 (80% reduction -> 0.2)")
        adjustment = {"hours": hours, "factor": clean_number(factor)}
    elif dtype == "minimum_battery_reserve":
        adjustment = {"hours": hours, "minimum_energy_kwh": clean_number(_reserve_kwh(raw, battery))}
    elif dtype == "max_grid_window":
        cap = _as_number(raw.get("max_grid_kwh"), "max_grid_kwh")
        if cap < 0:
            raise GuardrailError("max_grid_kwh must be non-negative")
        adjustment = {"hours": hours, "max_grid_kwh": clean_number(cap)}
    else:  # no_charge_window / no_discharge_window
        adjustment = {"hours": hours}

    return {
        "applies": True,
        "directive_type": dtype,
        "structured_adjustment": adjustment,
        "explanation": _explanation(raw, dtype),
    }
