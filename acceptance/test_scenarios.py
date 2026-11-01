import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session


@pytest.mark.skipif(os.getenv("SCENARIO") != "brownfield", reason="Expiration scenario only")
def test_expiration_contract(client, headers):
    from shortener.models import Link

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    response = client.post(
        "/api/links", headers=headers, json={"url": "https://example.com/", "expires_at": future}
    )
    assert response.status_code == 201, response.text
    code = response.json()["code"]
    assert client.get(f"/r/{code}", follow_redirects=False).status_code == 302
    with Session(client.app.state.engine) as db:
        link = db.get(Link, code)
        assert link.expires_at is not None, "Expiration must be persisted, not ignored"
        link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert client.get(f"/r/{code}", follow_redirects=False).status_code == 410
    assert client.get(f"/api/links/{code}/stats", headers=headers).json()["total_clicks"] == 1
    assert (
        client.post(
            "/api/links",
            headers=headers,
            json={"url": "https://example.com/", "expires_at": "2000-01-01T00:00:00Z"},
        ).status_code
        == 422
    )


@pytest.mark.skipif(
    os.getenv("SCENARIO") != "ambiguous", reason="Agreed domain policy scenario only"
)
def test_agreed_domain_policy(client, headers):
    for domain in ["malware.test", "a.malware.test", "MALWARE.test"]:
        assert (
            client.post(
                "/api/links", headers=headers, json={"url": f"https://{domain}/"}
            ).status_code
            == 422
        )
    for domain in ["notmalware.test", "example.com"]:
        assert (
            client.post(
                "/api/links", headers=headers, json={"url": f"https://{domain}/"}
            ).status_code
            == 201
        )
