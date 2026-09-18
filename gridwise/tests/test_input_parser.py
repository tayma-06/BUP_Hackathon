"""Input parsing and normalization tests (app.schemas.parse_scenario).

The API accepts one official schema: scenario_id, operator_notes (1-3),
hours (exactly 24) and battery. There are no optional fields; every field is
required, and nothing is silently defaulted. Unknown keys may appear in the
body but are ignored - they do not map to the official names.
"""
import pytest

from app.schemas import RequestError, parse_scenario


def test_official_schema_normalizes(body_builder):
    body = body_builder(notes=("solar reduced",))
    scenario = parse_scenario(body)
    assert scenario.scenario_id == "TEST-001"
    assert scenario.notes == ["solar reduced"]
    assert len(scenario.hours) == 24
    assert [h.hour for h in scenario.hours] == list(range(24))


def test_alternative_id_notes_keys_are_rejected(body_builder):
    # Some clients send {"id": ..., "notes": [...]}; this production schema
    # does NOT alias those keys, so the request must fail with a clear error.
    body = body_builder()
    body.pop("scenario_id")
    body["id"] = "A1"
    body["notes"] = body.pop("operator_notes")
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 400
    assert "scenario_id" in exc.value.message


@pytest.mark.parametrize("key", ["scenario_id", "operator_notes", "hours", "battery"])
def test_every_field_is_required(body_builder, key):
    body = body_builder()
    del body[key]
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 400


def test_missing_optional_fields_have_no_defaults(body_builder):
    # There are no optional request fields: dropping the trailing tariff entry
    # replaces a field with a validation error rather than a default.
    body = body_builder()
    del body["battery"]["minimum_energy_kwh"]
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 400


@pytest.mark.parametrize("count", [0, 4])
def test_note_count_bounds(body_builder, count):
    body = body_builder(notes=[f"note-{i}" for i in range(count)])
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 400


def test_blank_note_rejected(body_builder):
    body = body_builder(notes=("   ",))
    with pytest.raises(RequestError) as exc:
        parse_scenario(body)
    assert exc.value.status == 400


def test_notes_copied_not_referenced(body_builder):
    body = body_builder(notes=("solar reduced",))
    scenario = parse_scenario(body)
    body["operator_notes"][0] = "mutated"
    assert scenario.notes == ["solar reduced"]


def test_unsorted_hours_are_normalized_sorted(body_builder):
    body = body_builder()
    body["hours"] = list(reversed(body["hours"]))
    scenario = parse_scenario(body)
    assert [h.hour for h in scenario.hours] == list(range(24))


def test_irrelevant_extra_keys_ignored(body_builder):
    body = body_builder()
    body["extra_field"] = "survives"
    body["notes"] = ["ignored alias"]
    scenario = parse_scenario(body)
    assert scenario.scenario_id == "TEST-001"