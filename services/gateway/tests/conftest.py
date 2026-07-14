from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from gw_helpers import TEST_SECRET, FakeRasterizer

from newfan_gateway.admin import InMemoryAdminRepository
from newfan_gateway.app import create_app
from newfan_gateway.auth import InMemoryApiKeyStore, Principal
from newfan_gateway.config import Settings
from newfan_gateway.ports import FakeOrchestratorClient
from newfan_gateway.queue import InMemoryQueue
from newfan_gateway.records import RuleRecord, SchemaFieldDef, SchemaRecord
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
    admin = InMemoryAdminRepository()
    admin.seed_schema(
        SchemaRecord(
            id="sch_1",
            tenant_id="ten_1",
            doc_type="invoice",
            version=4,
            fields=[
                SchemaFieldDef(name="total_amount", label="合計金額(税込)", type="money_jpy", required=True, critical=True),
                SchemaFieldDef(name="issuer_name", label="取引先名", type="string", required=True, critical=True),
            ],
        )
    )
    admin.seed_rule(
        RuleRecord(
            id="rul_ok",
            tenant_id="ten_1",
            doc_type="invoice",
            field_name="total_amount",
            rule_type="regex_replace",
            rule_json={"pattern": "x"},
            status="draft",
            validation_report={"reproduction_rate": 1.0, "regressions": 0},
            source_correction_ids=["cor_1"],
        )
    )
    admin.seed_rule(
        RuleRecord(
            id="rul_bad",
            tenant_id="ten_1",
            rule_type="vocab_map",
            rule_json={},
            status="draft",
            validation_report={"reproduction_rate": 0.5, "regressions": 2},
        )
    )
    app = create_app(
        settings=Settings(jwt_secret=TEST_SECRET, storage_root=tmp_path),
        repo=repo,
        queue=queue,
        orchestrator=orch,
        ingestor=ingestor,
        api_keys=api_keys,
        admin=admin,
    )
    return SimpleNamespace(client=TestClient(app), repo=repo, queue=queue, orch=orch, admin=admin)
