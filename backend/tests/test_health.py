from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app


def test_health_reports_ok_and_mock_mode():
    app.dependency_overrides[get_settings] = lambda: Settings(mock_mode=True)
    try:
        res = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["mock_mode"] is True
