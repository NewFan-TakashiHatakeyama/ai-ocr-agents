from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from gw_helpers import TEST_SECRET, FakeRasterizer

from newfan_gateway.app import create_app
from newfan_gateway.auth import InMemoryApiKeyStore, Principal
from newfan_gateway.config import Settings
from newfan_gateway.ports import FakeOrchestratorClient
from newfan_gateway.queue import InMemoryQueue
from newfan_gateway.repository import InMemoryRepository
from newfan_ingest import IngestService
from newfan_ingest.storage import LocalObjectStore


@pytest.fixture
def ctx(tmp_path: Path) -> SimpleNamespace:
    repo = InMemoryRepository()
    queue = InMemoryQueue()
    orch = FakeOrchestratorClient()
    ingestor = IngestService(LocalObjectStore(tmp_path), FakeRasterizer())
    api_keys = InMemoryApiKeyStore(
        {"api-key-123": Principal(sub="m2m", tenant_id="ten_1", role="api")}
    )
    app = create_app(
        settings=Settings(jwt_secret=TEST_SECRET, storage_root=tmp_path),
        repo=repo,
        queue=queue,
        orchestrator=orch,
        ingestor=ingestor,
        api_keys=api_keys,
    )
    return SimpleNamespace(client=TestClient(app), repo=repo, queue=queue, orch=orch)
