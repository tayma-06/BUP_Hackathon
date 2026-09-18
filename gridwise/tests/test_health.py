"""GET /health endpoint contract tests."""
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "status" in data
    assert data["status"] == "ok"


def test_health_head_supported(client):
    response = client.head("/health")
    assert response.status_code == 200


def test_health_not_ready_without_provider(monkeypatch, fake_llm):
    monkeypatch.setattr(main, "settings", Settings((), 0.5, 2, 2, 20))
    with TestClient(main.app, raise_server_exceptions=False) as api:
        response = api.get("/health")
        assert response.status_code == 500
        assert response.json()["error"] == "not_ready"


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "GridWise LLM"
    assert "GET /health" in response.json()["endpoints"]