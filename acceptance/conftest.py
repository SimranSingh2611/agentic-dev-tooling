import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from shortener.main import create_app

    app = create_app(f"sqlite:///{tmp_path / 'contract.db'}", "acceptance-key", rate_limit=200)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def headers():
    return {"X-API-Key": "acceptance-key"}
