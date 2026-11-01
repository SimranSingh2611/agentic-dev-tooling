"""Run with TEST_POSTGRES_URL against a disposable PostgreSQL instance."""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from engineering.api import create_app as engineering_app
from engineering.store import Store
from engineering.worker import work_once
from engineering.workflow import build_graph, checkpointer
from shortener.main import create_app

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL not configured"
)


@pytest.fixture
def postgres_url():
    url = make_url(os.environ["TEST_POSTGRES_URL"])
    schema = "test_" + uuid.uuid4().hex
    engine = create_engine(url)
    with engine.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
            hide_password=False
        )
    finally:
        with engine.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_postgres_shortener_lifecycle(postgres_url):
    with TestClient(create_app(postgres_url, "pg-test")) as client:
        client.headers["X-API-Key"] = "pg-test"
        first = client.post(
            "/api/links",
            json={"url": "https://example.com/"},
            headers={"Idempotency-Key": "pg-request"},
        )
        assert first.status_code == 201
        code = first.json()["code"]
        assert client.get(f"/r/{code}", follow_redirects=False).status_code == 302
        assert client.get(f"/api/links/{code}/stats").json()["total_clicks"] == 1
        assert (
            client.post(
                "/api/links",
                json={"url": "https://example.com/"},
                headers={"Idempotency-Key": "pg-request"},
            ).status_code
            == 200
        )


def test_postgres_checkpoints_resume_after_restart(postgres_url, config):
    config.engineering_database_url = postgres_url
    config.checkpoint_database_url = postgres_url
    store = Store(postgres_url)
    with TestClient(engineering_app(config)) as client:
        client.headers["X-API-Key"] = config.engineering_api_key
        response = client.post(
            "/engineering/runs",
            json={
                "requirement": "Add optional expiration to shortened links.",
                "mode": "demo",
                "scenario": "brownfield",
            },
        )
        run_id = response.json()["id"]
        with checkpointer(config) as saver:
            work_once(config, store, build_graph(config, store, saver))
        run = store.get(run_id)
        assert run.status == "awaiting_approval", run.state
        response = client.post(
            f"/engineering/runs/{run_id}/approval",
            json={
                "revision": 1,
                "candidate_hash": run.state["candidate_hash"],
                "approved": True,
                "reviewer": "postgres-test",
            },
        )
        assert response.status_code == 202
        with checkpointer(config) as saver:
            work_once(config, store, build_graph(config, store, saver))
        assert store.get(run_id).status == "completed"
        assert store.audit_valid(run_id)
    store.engine.dispose()
