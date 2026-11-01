def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_create_redirect_and_analytics(client, headers):
    response = client.post(
        "/api/links", headers=headers, json={"url": "https://example.com/document"}
    )
    assert response.status_code == 201, response.text
    link = response.json()
    response = client.get(f"/r/{link['code']}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/document"
    assert (
        client.get(f"/api/links/{link['code']}/stats", headers=headers).json()["total_clicks"] == 1
    )


def test_authentication_and_validation(client, headers):
    assert client.post("/api/links", json={"url": "https://example.com"}).status_code == 401
    for url in ["javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com"]:
        assert client.post("/api/links", headers=headers, json={"url": url}).status_code == 422


def test_missing_link(client, headers):
    assert client.get("/r/missing", follow_redirects=False).status_code == 404
    assert client.get("/api/links/missing/stats", headers=headers).status_code == 404


def test_idempotency(client, headers):
    headers = {**headers, "Idempotency-Key": "same-request"}
    body = {"url": "https://example.com/"}
    first = client.post("/api/links", headers=headers, json=body)
    second = client.post("/api/links", headers=headers, json=body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["code"] == second.json()["code"]
    assert (
        client.post(
            "/api/links", headers=headers, json={"url": "https://other.example/"}
        ).status_code
        == 409
    )


def test_creation_rate_limit_contract(tmp_path):
    from fastapi.testclient import TestClient

    from shortener.main import create_app

    with TestClient(
        create_app(f"sqlite:///{tmp_path / 'rate.db'}", "rate-key", rate_limit=2)
    ) as client:
        client.headers["X-API-Key"] = "rate-key"
        for _ in range(2):
            assert (
                client.post("/api/links", json={"url": "https://example.com/"}).status_code == 201
            )
        assert client.post("/api/links", json={"url": "https://example.com/"}).status_code == 429
