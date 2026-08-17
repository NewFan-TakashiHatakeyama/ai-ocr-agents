"""graph_json モデルの契約（§4.1〜4.2）。

「未知のものは保存時に弾く」を固定する。実行時に初めて落ちると、
有効化済みのワークフローが本番で死ぬ。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from newfan_workflow import NODE_CONFIG_MODELS, WorkflowGraph, catalog

# 設計書 §16.1 の代表例（S3 監視 → 抽出 → 分岐 → HITL/DB → 通知）を完結させた形
VALID: dict[str, Any] = {
    "version": 1,
    "nodes": [
        {
            "id": "n1",
            "type": "source.s3_event",
            "config": {"connection_id": "con_s3", "prefix": "invoices/"},
            "pos": [16, 100],
        },
        {"id": "n3", "type": "process.extract", "config": {"schema_id": "sch_invoice_v4"}},
        {
            "id": "n4",
            "type": "branch.condition",
            "config": {
                "branches": [{"when": "run.status == 'needs_review'", "to": "n5"}],
                "else": "n6",
            },
        },
        {"id": "n5", "type": "branch.hitl_gate", "config": {}},
        {
            "id": "n6",
            "type": "transform.map_fields",
            "config": {
                "mappings": [
                    {"from": "invoice_no", "to": "invoice_no"},
                    {"from": "total_amount", "to": "amount", "format": "number"},
                    {"const": "ai-ocr", "to": "source_system"},
                ]
            },
        },
        {
            "id": "n7",
            "type": "sink.db_write",
            "config": {
                "connection_id": "con_pg",
                "table": "erp.invoices",
                "mode": "upsert",
                "keys": ["invoice_no"],
                "on_failure": "halt_notify",
            },
        },
    ],
    "edges": [
        {"from": "n1", "to": "n3"},
        {"from": "n3", "to": "n4"},
        {"from": "n4", "to": "n5", "label": "要確認あり"},
        {"from": "n4", "to": "n6"},
        {"from": "n5", "to": "n6"},
        {"from": "n6", "to": "n7"},
    ],
}


def _mutate(**changes: Any) -> dict[str, Any]:
    import copy

    g = copy.deepcopy(VALID)
    g.update(changes)
    return g


def test_代表例がparseできる() -> None:
    g = WorkflowGraph.model_validate(VALID)
    assert g.node_ids() == {"n1", "n3", "n4", "n5", "n6", "n7"}
    node = g.get("n4")
    assert node is not None and node.type == "branch.condition"


def test_エイリアスでラウンドトリップできる() -> None:
    # "else" と "from" は Python の予約語。dump は alias で出し、元 JSON と同じ形に戻す
    g = WorkflowGraph.model_validate(VALID)
    dumped = g.model_dump(by_alias=True, exclude_none=True)
    assert dumped["nodes"][2]["config"]["else"] == "n6"
    assert dumped["edges"][0]["from"] == "n1"
    assert WorkflowGraph.model_validate(dumped).node_ids() == g.node_ids()


def test_未知のノード種別は保存時に弾く() -> None:
    g = _mutate(
        nodes=VALID["nodes"] + [{"id": "nx", "type": "sink.ftp_upload", "config": {}}]
    )
    with pytest.raises(ValidationError):
        WorkflowGraph.model_validate(g)


def test_未知のconfigキーは弾く() -> None:
    # typo（interval_secs 等）が黙って既定値になるのを防ぐ
    g = _mutate()
    g["nodes"][1]["config"]["schema"] = "sch_x"  # 正しくは schema_id
    with pytest.raises(ValidationError):
        WorkflowGraph.model_validate(g)


def test_node_idの重複は弾く() -> None:
    g = _mutate()
    g["nodes"][1]["id"] = "n1"
    with pytest.raises(ValidationError, match="重複"):
        WorkflowGraph.model_validate(g)


def test_versionは1のみ() -> None:
    with pytest.raises(ValidationError):
        WorkflowGraph.model_validate(_mutate(version=2))


def test_extractはschema_id必須() -> None:
    # 無いと load_context が空スキーマを返し、1 項目も抽出しないまま「成功」する
    g = _mutate()
    g["nodes"][1]["config"] = {}
    with pytest.raises(ValidationError):
        WorkflowGraph.model_validate(g)


def test_upsertはkeys必須でinsertはkeys禁止() -> None:
    g = _mutate()
    g["nodes"][5]["config"]["keys"] = []
    with pytest.raises(ValidationError, match="keys"):
        WorkflowGraph.model_validate(g)

    g = _mutate()
    g["nodes"][5]["config"]["mode"] = "insert"  # keys が残ったまま
    with pytest.raises(ValidationError, match="insert"):
        WorkflowGraph.model_validate(g)


@pytest.mark.parametrize(
    "table",
    [
        "erp.invoices; DROP TABLE users",  # インジェクション
        "erp.invoices--",
        'erp."invoices"',
        "erp.invoices.extra",  # 3 階層
        "1invoices",  # 数字始まり
    ],
)
def test_識別子でないテーブル名は弾く(table: str) -> None:
    g = _mutate()
    g["nodes"][5]["config"]["table"] = table
    with pytest.raises(ValidationError):
        WorkflowGraph.model_validate(g)


def test_識別子でないキー列は弾く() -> None:
    g = _mutate()
    g["nodes"][5]["config"]["keys"] = ["invoice_no", "no; --"]
    with pytest.raises(ValidationError, match="識別子"):
        WorkflowGraph.model_validate(g)


def test_不正な条件式は保存時に弾く() -> None:
    # 実行時に初めて落ちると、有効化済みワークフローが本番で死ぬ
    g = _mutate()
    g["nodes"][2]["config"]["branches"][0]["when"] = "run.status > 5"
    with pytest.raises(ValidationError):
        WorkflowGraph.model_validate(g)


def test_mappingはfromとconstのどちらか一方() -> None:
    g = _mutate()
    g["nodes"][4]["config"]["mappings"][0] = {"from": "a", "const": "b", "to": "c"}
    with pytest.raises(ValidationError, match="どちらか一方"):
        WorkflowGraph.model_validate(g)

    g = _mutate()
    g["nodes"][4]["config"]["mappings"][0] = {"to": "c"}
    with pytest.raises(ValidationError, match="どちらか一方"):
        WorkflowGraph.model_validate(g)


def test_scheduleのcronは5フィールド() -> None:
    g = _mutate(
        nodes=[{"id": "t", "type": "source.schedule", "config": {"cron": "0 9 * *"}}],
        edges=[],
    )
    with pytest.raises(ValidationError, match="cron"):
        WorkflowGraph.model_validate(g)


def test_catalogは全ノード種別のJSONSchemaを返す() -> None:
    # SCR-07 の設定フォームはここから自動生成する（手書きしない）
    schemas = catalog()
    assert set(schemas) == set(NODE_CONFIG_MODELS)
    assert len(schemas) == 14  # 13 + source.gdrive_event（⑤⑥）
    assert "schema_id" in schemas["process.extract"]["required"]
    # extra=forbid が JSON Schema にも出る（フォーム側で未知キーを出さない）
    assert schemas["process.extract"]["additionalProperties"] is False


# ---- ⑤⑥ SaaS連携: source.gdrive_event（2026-08-17） ----


def test_gdrive_eventノードを読める() -> None:
    from newfan_workflow import WorkflowGraph

    g = WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [
                {"id": "t1", "type": "source.gdrive_event",
                 "config": {"connection_id": "con_gd"}},
                {"id": "x1", "type": "process.extract", "config": {"schema_id": "s1"}},
            ],
            "edges": [{"from": "t1", "to": "x1"}],
        }
    )
    node = g.get("t1")
    assert node is not None and node.type == "source.gdrive_event"
    # 既定の拡張子フィルタ（S3 トリガーと同じ）
    assert ".pdf" in node.config.extensions


def test_gdrive_eventはカタログに載る() -> None:
    from newfan_workflow import catalog

    assert "source.gdrive_event" in catalog()


def test_gdrive_eventの拡張子はドット始まり必須() -> None:
    import pytest
    from newfan_workflow.models import GDriveEventConfig

    with pytest.raises(Exception):
        GDriveEventConfig(connection_id="c", extensions=["pdf"])
