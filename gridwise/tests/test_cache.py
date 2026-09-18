"""Request-context interpretation cache (app.interpreter.NoteCache) + API-level caching."""
from dataclasses import replace

from app.interpreter import NoteCache
from app.schemas import Battery


def noop(index=0):
    return {"note_index": index, "applies": False, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "Unrelated to energy."}


def test_cache_returns_deep_copy():
    cache = NoteCache(4)
    battery = Battery(220, 110, 40, 50, 50)
    cache.put(["half full"], battery, [noop(0)])

    first = cache.get(["half full"], battery)
    assert first == [noop(0)]
    first[0]["directive_type"] = "mutated"
    assert cache.get(["half full"], battery) == [noop(0)]


def test_cache_misses_on_note_text_change():
    cache = NoteCache(4)
    battery = Battery(220, 110, 40, 50, 50)
    cache.put(["half full"], battery, [noop(0)])
    assert cache.get(["half full "], battery) is None  # whitespace differs
    assert cache.get(["half full", "extra"], battery) is None  # list differs


def test_cache_misses_on_battery_change():
    cache = NoteCache(4)
    battery = Battery(220, 110, 40, 50, 50)
    cache.put(["half full"], battery, [noop(0)])
    assert cache.get(["half full"], replace(battery, capacity=400)) is None
    assert cache.get(["half full"], replace(battery, initial=120)) is None


def test_cache_evicts_oldest_when_full():
    cache = NoteCache(2)
    battery = Battery(220, 110, 40, 50, 50)
    cache.put(["a"], battery, [noop(0)])
    cache.put(["b"], battery, [noop(0)])
    cache.put(["c"], battery, [noop(0)])
    assert cache.get(["a"], battery) is None  # LRU evicted
    assert cache.get(["b"], battery) is not None
    assert cache.get(["c"], battery) is not None


def test_cache_ordering_moves_recent_to_end():
    cache = NoteCache(2)
    battery = Battery(220, 110, 40, 50, 50)
    cache.put(["a"], battery, [noop(0)])
    cache.put(["b"], battery, [noop(0)])
    cache.get(["a"], battery)  # touch oldest -> now most recent
    cache.put(["c"], battery, [noop(0)])
    assert cache.get(["a"], battery) is not None
    assert cache.get(["b"], battery) is None


def test_cache_max_size_zero_disabled():
    cache = NoteCache(0)
    battery = Battery(1, 0, 0, 1, 1)
    cache.put(["x"], battery, [noop(0)])
    assert cache.get(["x"], battery) is None


def test_api_reuses_cache_for_identical_requests(client, fake_llm, body_builder):
    body = body_builder(notes=("Solar output drops to 25% from noon to 2 PM.",))
    first = client.post("/optimize-energy", json=body)
    assert first.status_code == 200
    assert len(fake_llm) == 1

    second = client.post("/optimize-energy", json=body)
    assert second.status_code == 200
    assert len(fake_llm) == 1  # served from cache, no new LLM call
    assert "cache:llm" in second.headers["X-Interpreter-Sources"]
    assert second.json() == first.json()


def test_api_cache_misses_on_different_notes(client, fake_llm, body_builder):
    note_a = "Solar output drops to 25% from noon to 2 PM."
    note_b = "Do not charge between 2 PM and 4 PM."
    assert client.post("/optimize-energy", json=body_builder(notes=(note_a,))).status_code == 200
    assert len(fake_llm) == 1
    assert client.post("/optimize-energy", json=body_builder(notes=(note_b,))).status_code == 200
    assert len(fake_llm) == 2  # different prompt context -> cached separately


def test_api_cache_misses_on_different_battery(client, fake_llm, body_builder, battery_builder):
    body_a = body_builder()
    body_b = body_builder(battery=battery_builder(initial_energy_kwh=120))
    assert client.post("/optimize-energy", json=body_a).status_code == 200
    assert len(fake_llm) == 1
    assert client.post("/optimize-energy", json=body_b).status_code == 200
    assert len(fake_llm) == 2
    again = client.post("/optimize-energy", json=body_a)
    assert "cache:llm" in again.headers["X-Interpreter-Sources"]
    assert len(fake_llm) == 2  # body_a hit the cache again