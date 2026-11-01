from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engineering.api import create_app
from engineering.config import Settings
from engineering.store import Store


@pytest.fixture
def config(tmp_path):
    return Settings(
        _env_file=None,
        engineering_database_url=f"sqlite:///{tmp_path / 'engineering.db'}",
        checkpoint_database_url=f"sqlite:///{tmp_path / 'checkpoints.db'}",
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspaces",
        source_root=Path(__file__).resolve().parents[1],
        runner_backend="local",
        openai_api_key="",
        worker_lease_seconds=30,
    )


@pytest.fixture
def store(config):
    store = Store(config.engineering_database_url)
    store.initialize()
    yield store
    store.engine.dispose()


@pytest.fixture
def api(config):
    with TestClient(create_app(config)) as client:
        client.headers["X-API-Key"] = config.engineering_api_key
        yield client


@pytest.fixture
def live_patch(config):
    """A complete simulated model response; trusted acceptance still runs separately."""
    from engineering.agents import demo_patch
    from engineering.schemas import FileChange

    patch = demo_patch(config.source_root, "brownfield")
    patch.files.extend(
        [
            FileChange(
                path="README.md",
                content="# Link expiration\n\nSet expires_at to a future timezone-aware ISO timestamp. Expired redirects return 410 without recording a click. Existing databases require a separately reviewed migration adding a nullable timestamp column.\n",
            ),
            FileChange(
                path="tests/test_expiration.py",
                content="""from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from shortener.main import create_app


def test_naive_expiration_rejected(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path / 'expiry.db'}", api_key="test-key")) as client:
        response = client.post("/api/links", headers={"X-API-Key": "test-key"}, json={
            "url": "https://example.com/path",
            "expires_at": (datetime.now() + timedelta(days=1)).isoformat(),
        })
        assert response.status_code == 422
""",
            ),
        ]
    )
    return patch
