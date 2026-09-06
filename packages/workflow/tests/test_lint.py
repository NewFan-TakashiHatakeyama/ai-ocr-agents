"""構成 lint の全規則（§5）。

各規則について「検知する」と「正常なら黙る」の両方を固定する。
規則 ID（L001〜L010）は UI との契約なのでテストでも固定する。
"""

from __future__ import annotations

import copy
from typing import Any

from newfan_workflow import WorkflowGraph, has_errors, lint
from newfan_workflow.lint import Finding

# 代表例（test_models.VALID と同型）: 監視 → 抽出 → 分岐(run.status) → HITL/マッピング → DB
CLEAN: dict[str, Any] = {
    "version": 1,
    "nodes": [
        {
            "id": "t1",
            "type": "source.s3_event",
            "config": {"connection_id": "con_s3", "prefix": "invoices/"},
        },
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
        {
            "id": "b1",
            "type": "branch.condition",
            "config": {
                "branches": [{"when": "run.status == 'needs_review'", "to": "h1"}],
                "else": "m1",
            },
        },
        {"id": "h1", "type": "branch.hitl_gate", "config": {}},
        {
            "id": "m1",
            "type": "transform.map_fields",
            "config": {
                "mappings": [
                    {"from": "invoice_no", "to": "invoice_no"},
                    {"from": "total_amount", "to": "amount"},
                ]
            },
        },
        {
            "id": "s1",
            "type": "sink.db_write",
            "config": {
                "connection_id": "con_pg",
                "table": "erp.invoices",
                "mode": "upsert",
                "keys": ["invoice_no"],
            },
        },
    ],
    "edges": [
        {"from": "t1", "to": "x1"},
        {"from": "x1", "to": "b1"},
        {"from": "b1", "to": "h1"},
        {"from": "b1", "to": "m1"},
        {"from": "h1", "to": "m1"},
        {"from": "m1", "to": "s1"},
    ],
}


def _graph(**changes: Any) -> WorkflowGraph:
    data = copy.deepcopy(CLEAN)
    data.update(changes)
    return WorkflowGraph.model_validate(data)


def _rules(findings: list[Finding]) -> set[str]:
    return {f.rule for f in findings}


def test_代表例は指摘ゼロ() -> None:
    # 設計書 §16.1 の代表形（needs_review→HITL / else→sink）が偽陽性を出さないこと。
    # とくに L008 は「run.status を見る分岐」を通過する経路に出してはいけない。
    findings = lint(_graph())
    assert findings == []


def test_L001_閉路を検知する() -> None:
    data = copy.deepcopy(CLEAN)
    data["edges"].append({"from": "s1", "to": "x1"})  # 出力から抽出へ戻る輪
    findings = lint(WorkflowGraph.model_validate(data))
    assert "L001" in _rules(findings)
    f = next(f for f in findings if f.rule == "L001")
    assert "→" in f.message  # どこが回っているかを見せる


def test_L002_トリガーが無いと検知する() -> None:
    data = copy.deepcopy(CLEAN)
    data["nodes"] = data["nodes"][1:]  # t1 を除去
    data["edges"] = [e for e in data["edges"] if e["from"] != "t1"]
    findings = lint(WorkflowGraph.model_validate(data))
    assert "L002" in _rules(findings)
    # トリガーが無い時に L003（全ノード到達不能）を重ねて出さない
    assert "L003" not in _rules(findings)


def test_L003_到達不能ノードを検知する() -> None:
    data = copy.deepcopy(CLEAN)
    data["nodes"].append(
        {"id": "orphan", "type": "sink.notify",
         "config": {"connection_id": "con_slack", "template": "done"}}
    )
    findings = lint(WorkflowGraph.model_validate(data))
    l003 = [f for f in findings if f.rule == "L003"]
    assert [f.node_id for f in l003] == ["orphan"]


def test_L004_DB出力の上流に抽出が無いと検知する() -> None:
    data = copy.deepcopy(CLEAN)
    # t1 → s1 を直結した別の sink（抽出を経ない）
    data["nodes"].append(
        {"id": "s2", "type": "sink.file",
         "config": {"connection_id": "con_s3", "path": "out/", "format": "csv"}}
    )
    data["edges"].append({"from": "t1", "to": "s2"})
    findings = lint(WorkflowGraph.model_validate(data))
    assert any(f.rule == "L004" and f.node_id == "s2" for f in findings)
    # 既存の s1（抽出経由）には出ない
    assert not any(f.rule == "L004" and f.node_id == "s1" for f in findings)


def test_L005_はモデル検証をバイパスした構築でも検知する() -> None:
    # 通常は pydantic が else 必須を強制する。model_construct 等で検証を飛ばした
    # 場合の防御として lint 側にも残している（規則 ID は設計の契約）。
    g = _graph()
    cond = g.get("b1")
    assert cond is not None
    object.__setattr__(cond.config, "else_", "")
    findings = lint(g)
    assert any(f.rule == "L005" and f.node_id == "b1" for f in findings)


def test_L006_存在しない参照を検知する() -> None:
    data = copy.deepcopy(CLEAN)
    data["edges"].append({"from": "s1", "to": "ghost"})
    data["nodes"][2]["config"]["else"] = "ghost2"
    findings = lint(WorkflowGraph.model_validate(data))
    msgs = [f.message for f in findings if f.rule == "L006"]
    assert any("ghost" in m for m in msgs)
    assert any("ghost2" in m for m in msgs)


def test_L007_マッピング未定義を警告する() -> None:
    data = copy.deepcopy(CLEAN)
    # m1 を飛ばして h1 → s1 直結にする
    data["edges"] = [e for e in data["edges"] if not (e["from"] == "m1" or e["to"] == "m1")]
    data["edges"] += [{"from": "b1", "to": "s1"}, {"from": "h1", "to": "s1"}]
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "m1"]
    data["nodes"][2]["config"]["else"] = "s1"
    findings = lint(WorkflowGraph.model_validate(data))
    assert any(f.rule == "L007" and f.severity == "warning" for f in findings)


def test_L007_upsertキーがマッピングに無いと警告する() -> None:
    data = copy.deepcopy(CLEAN)
    data["nodes"][5]["config"]["keys"] = ["invoice_no", "issuer_code"]  # issuer_code は未生成
    findings = lint(WorkflowGraph.model_validate(data))
    f = next(f for f in findings if f.rule == "L007")
    assert "issuer_code" in f.message
    assert "invoice_no" not in f.message  # 生成済みの列は指摘しない


def test_L008_HITLを経ない経路を警告する() -> None:
    data = copy.deepcopy(CLEAN)
    # 分岐を外して抽出 → マッピング → DB 直行にする（needs_review の行き場が無い）
    data["nodes"] = [n for n in data["nodes"] if n["id"] not in ("b1", "h1")]
    data["edges"] = [
        {"from": "t1", "to": "x1"},
        {"from": "x1", "to": "m1"},
        {"from": "m1", "to": "s1"},
    ]
    findings = lint(WorkflowGraph.model_validate(data))
    assert any(f.rule == "L008" and f.node_id == "s1" for f in findings)


def test_L008_auto_confirmがtrueなら出さない() -> None:
    data = copy.deepcopy(CLEAN)
    data["nodes"] = [n for n in data["nodes"] if n["id"] not in ("b1", "h1")]
    data["edges"] = [
        {"from": "t1", "to": "x1"},
        {"from": "x1", "to": "m1"},
        {"from": "m1", "to": "s1"},
    ]
    findings = lint(WorkflowGraph.model_validate(data), auto_confirm=True)
    assert "L008" not in _rules(findings)


def test_L009_スキーマの実在を検査する() -> None:
    findings = lint(_graph(), schema_exists=lambda sid: sid == "sch_other")
    assert any(f.rule == "L009" and f.node_id == "x1" for f in findings)
    # resolver 未注入なら skip（オフライン lint）
    assert "L009" not in _rules(lint(_graph()))


def test_L012_旧版のスキーマ参照をwarningで知らせる() -> None:
    """extract.schema_id が最新版でないことを warning で出す。

    put_schema は常に新版 INSERT だが、ワークフローの extract ノードは
    field_schemas.id を固定保持する。したがってテンプレート化や領域編集で作った
    新版は既存ワークフローに自動適用されず、運用者は「保存したのに効かない」を
    無音で踏む（L009 は存在するかしか見ないので警告も出ない）。
    """
    findings = lint(_graph(), schema_is_latest=lambda sid: False)
    l012 = [f for f in findings if f.rule == "L012"]
    assert [f.node_id for f in l012] == ["x1"]
    # **error にはしない**（既存ワークフローの有効化を塞ぐ副作用の方が大きい）
    assert l012[0].severity == "warning"
    # 最新版なら出さない / resolver 未注入なら skip
    assert "L012" not in _rules(lint(_graph(), schema_is_latest=lambda sid: True))
    assert "L012" not in _rules(lint(_graph()))


def test_L010_接続の実在と疎通を検査する() -> None:
    findings = lint(_graph(), connection_ok=lambda cid: cid == "con_pg")
    l010 = [f for f in findings if f.rule == "L010"]
    assert [f.node_id for f in l010] == ["t1"]  # con_s3 だけが NG
    assert "L010" not in _rules(lint(_graph()))


def test_errorがwarningより先に並ぶ() -> None:
    data = copy.deepcopy(CLEAN)
    data["edges"].append({"from": "s1", "to": "ghost"})  # L006 error
    data["nodes"][5]["config"]["keys"] = ["invoice_no", "missing_col"]  # L007 warning
    findings = lint(WorkflowGraph.model_validate(data))
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: s != "error")
    assert has_errors(findings)


def test_指摘ゼロならhas_errorsはFalse() -> None:
    assert has_errors([]) is False


# ---- ⑤⑥ L011: gdrive トリガーの重複（2026-08-17 レビュー確定） ----


def _gd_graph(nodes, edges):
    from newfan_workflow import WorkflowGraph

    return WorkflowGraph.model_validate({"version": 1, "nodes": nodes, "edges": edges})


def test_L011_同一接続の重複gdriveトリガーはerror() -> None:
    from newfan_workflow import lint

    g = _gd_graph(
        [
            {"id": "t1", "type": "source.gdrive_event", "config": {"connection_id": "con_gd"}},
            {"id": "t2", "type": "source.gdrive_event", "config": {"connection_id": "con_gd"}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": "s1"}},
        ],
        [{"from": "t1", "to": "x1"}, {"from": "t2", "to": "x1"}],
    )
    findings = lint(g)
    assert any(f.rule == "L011" and f.severity == "error" for f in findings)


def test_L011_接続が別なら重複ではない() -> None:
    from newfan_workflow import lint

    g = _gd_graph(
        [
            {"id": "t1", "type": "source.gdrive_event", "config": {"connection_id": "con_a"}},
            {"id": "t2", "type": "source.gdrive_event", "config": {"connection_id": "con_b"}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": "s1"}},
        ],
        [{"from": "t1", "to": "x1"}, {"from": "t2", "to": "x1"}],
    )
    assert not any(f.rule == "L011" for f in lint(g))


def test_L011_拡張子が重ならなければ許容() -> None:
    from newfan_workflow import lint

    g = _gd_graph(
        [
            {"id": "t1", "type": "source.gdrive_event",
             "config": {"connection_id": "con_gd", "extensions": [".pdf"]}},
            {"id": "t2", "type": "source.gdrive_event",
             "config": {"connection_id": "con_gd", "extensions": [".png"]}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": "s1"}},
        ],
        [{"from": "t1", "to": "x1"}, {"from": "t2", "to": "x1"}],
    )
    assert not any(f.rule == "L011" for f in lint(g))
