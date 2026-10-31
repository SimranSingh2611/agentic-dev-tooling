from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from shortener.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path / 'shortener.db'}", "test-key")) as client:
        client.headers["X-API-Key"] = "test-key"
        yield client


def test_link_lifecycle_and_analytics(client):
    created = client.post("/api/links", json={"url": "https://example.com/article"})
    assert created.status_code == 201
    code = created.json()["code"]
    for _ in range(3):
        response = client.get(f"/r/{code}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "https://example.com/article"
        assert response.headers["cache-control"] == "no-store"
    stats = client.get(f"/api/links/{code}/stats").json()
    assert stats["total_clicks"] == 3
    assert sum(day["clicks"] for day in stats["daily"]) == 3


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.com",
        "https://user:password@example.com",
        "nonsense",
    ],
)
def test_rejects_unsafe_url_forms(client, url):
    assert client.post("/api/links", json={"url": url}).status_code == 422


def test_authentication(client):
    client.headers.pop("X-API-Key")
    assert client.post("/api/links", json={"url": "https://example.com"}).status_code == 401
    assert client.get("/api/links/anything/stats").status_code == 401


def test_idempotency(client):
    body = {"url": "https://example.com/"}
    headers = {"Idempotency-Key": "request-1"}
    first = client.post("/api/links", json=body, headers=headers)
    assert first.status_code == 201
    replay = client.post("/api/links", json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["code"] == first.json()["code"]
    assert (
        client.post(
            "/api/links", json={"url": "https://different.example"}, headers=headers
        ).status_code
        == 409
    )


def test_empty_idempotency_key_is_rejected(client):
    assert (
        client.post(
            "/api/links", headers={"Idempotency-Key": ""}, json={"url": "https://example.com/"}
        ).status_code
        == 422
    )


def test_random_collision_is_retried(client, monkeypatch):
    codes = iter(["collision", "collision", "unique"])
    monkeypatch.setattr("shortener.main.secrets.token_urlsafe", lambda _: next(codes))
    assert (
        client.post("/api/links", json={"url": "https://example.com"}).json()["code"] == "collision"
    )
    assert client.post("/api/links", json={"url": "https://example.org"}).json()["code"] == "unique"


def test_rate_limit_is_database_backed(tmp_path):
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    with (
        TestClient(create_app(url, "key", rate_limit=2)) as first,
        TestClient(create_app(url, "key", rate_limit=2)) as second,
    ):
        assert (
            first.post(
                "/api/links", headers={"X-API-Key": "key"}, json={"url": "https://example.com"}
            ).status_code
            == 201
        )
        assert (
            second.post(
                "/api/links", headers={"X-API-Key": "key"}, json={"url": "https://example.com"}
            ).status_code
            == 201
        )
        assert (
            first.post(
                "/api/links", headers={"X-API-Key": "key"}, json={"url": "https://example.com"}
            ).status_code
            == 429
        )


def test_concurrent_clicks_are_not_lost(client):
    code = client.post("/api/links", json={"url": "https://example.com"}).json()["code"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: client.get(f"/r/{code}", follow_redirects=False), range(12))
        )
    assert all(response.status_code == 302 for response in responses)
    assert client.get(f"/api/links/{code}/stats").json()["total_clicks"] == 12
