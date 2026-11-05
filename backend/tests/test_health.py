from app.clients import MockSearchClient
from app.mode import Mode, get_mode


class _DownLLM:
    def ping(self):
        return False, "unreachable: ConnectError"

    def list_models(self):
        raise AssertionError("not called")


def test_health_all_components_ok_in_mock_mode(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["mock_mode"] is True
    assert body["live_available"] is False and body["public_deployment"] is False
    assert body["components"]["database"] == {"ok": True, "detail": "ok"}
    assert body["components"]["llm"] == {"ok": True, "detail": "mock"}
    assert body["components"]["search"] == {"ok": True, "detail": "mock"}


def test_health_degraded_when_gateway_down(client):
    from app.main import app

    app.dependency_overrides[get_mode] = lambda: Mode(live=True, llm=_DownLLM(), search=MockSearchClient())
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["mock_mode"] is False
    assert body["components"]["llm"]["ok"] is False
    assert "unreachable" in body["components"]["llm"]["detail"]


def test_models_endpoint_lists_mock_models(client):
    body = client.get("/models").json()
    assert body["mock_mode"] is True
    assert "groq/openai/gpt-oss-20b" in body["models"]
