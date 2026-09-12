"""チャット承認カードの実行（POST /chat/confirm）: action ごとの実行・RBAC・形の一致。

以前の /chat/confirm は update_schema しか受けず、supervisor（chat_graph）が出す
rerun_extract / manage_rules の承認カードは「承認して実行」を押すと必ず E1003 で
失敗していた。ここでは 3 つの action それぞれの成功・権限不足・不正入力と、
**グラフが実際に出した confirm_request をそのまま流し込む** end-to-end を固定する。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from gw_helpers import auth

from newfan_gateway import dto
from newfan_gateway.chat_graph import TOOL_SPECS
from newfan_gateway.chat_tools import WRITE_TOOL_MIN_ROLE, WRITE_TOOLS
from newfan_gateway.records import DocumentRecord, PageRecord, RunRecord

# ---- helpers ----


def _seed_document(
    ctx: SimpleNamespace,
    *,
    run_status: str | None = "needs_review",
    run_options: dict[str, Any] | None = None,
) -> None:
    ctx.repo.create_document(
        DocumentRecord(
            id="doc_1", tenant_id="ten_1", storage_uri="s3://b/k", mime_type="image/png",
            page_count=1, doc_type="invoice", status=run_status or "uploaded",
        ),
        [PageRecord(page_no=1, width=740, height=1046, image_uri="s3://b/p1.png")],
    )
    if run_status:
        ctx.repo.create_run(
            RunRecord(
                id="run_1", tenant_id="ten_1", document_id="doc_1", status=run_status,
                options=run_options or {},
                # 新 run より確実に古くする（get_latest_run は started_at 降順）
                started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )


def _confirm(ctx: SimpleNamespace, role: str, action: str, params: dict[str, Any]) -> Any:
    return ctx.client.post(
        "/v1/chat/confirm", headers=auth(role), json={"action": action, "params": params}
    )


def _runs(ctx: SimpleNamespace) -> dict[str, RunRecord]:
    return dict(ctx.repo._runs)


# ---- rerun_extract（uploader 以上） ----


def test_rerun_extract_as_uploader_issues_run_and_supersedes_review(ctx: SimpleNamespace) -> None:
    _seed_document(ctx, run_status="needs_review")
    r = _confirm(ctx, "uploader", "rerun_extract", {"document_id": "doc_1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"]["document_id"] == "doc_1"
    assert body["detail"]["job_id"] and body["detail"]["run_id"]
    # 実際に Run が発行されてキューに入る
    assert len(ctx.queue.messages) == 1 and ctx.queue.messages[0][0] == "q.extract"
    assert ctx.queue.messages[0][1]["run_id"] == body["detail"]["run_id"]
    # 旧 needs_review は superseded に終端し、新 run が最新になる
    runs = _runs(ctx)
    assert runs["run_1"].status == "superseded"
    assert runs[body["detail"]["run_id"]].status == "processing"
    assert ctx.repo.get_latest_run("ten_1", "doc_1").id == body["detail"]["run_id"]
    assert ctx.repo.get_document("ten_1", "doc_1").status == "queued"


def test_rerun_extract_inherits_workflow_options_from_old_run(ctx: SimpleNamespace) -> None:
    """ワークフロー起点の run を置き換えるとき、再開先（workflow_notify）を落とさない。"""
    _seed_document(
        ctx,
        run_status="needs_review",
        run_options={"workflow_notify": {"run_id": "wfr_1"}, "workflow_idem": "idem-1", "other": 1},
    )
    r = _confirm(ctx, "uploader", "rerun_extract", {"document_id": "doc_1"})
    new_run = _runs(ctx)[r.json()["detail"]["run_id"]]
    assert new_run.options["workflow_notify"] == {"run_id": "wfr_1"}
    assert new_run.options["workflow_idem"] == "idem-1"
    assert "other" not in new_run.options


def test_rerun_extract_with_schema_id(ctx: SimpleNamespace) -> None:
    _seed_document(ctx)
    r = _confirm(ctx, "uploader", "rerun_extract", {"document_id": "doc_1", "schema_id": "sch_1"})
    assert r.json()["ok"] is True
    assert _runs(ctx)[r.json()["detail"]["run_id"]].schema_id == "sch_1"


@pytest.mark.parametrize("role,status", [("viewer", 403), ("uploader", 200), ("reviewer", 200), ("admin", 200)])
def test_rerun_extract_role_gate_matches_extract_endpoint(
    ctx: SimpleNamespace, role: str, status: int
) -> None:
    """POST /documents/{id}/extract と同じ uploader 以上。"""
    _seed_document(ctx)
    r = _confirm(ctx, role, "rerun_extract", {"document_id": "doc_1"})
    assert r.status_code == status, r.text
    if status == 403:
        assert r.json()["error"]["code"] == "E5001"
        assert ctx.queue.messages == []
        assert _runs(ctx)["run_1"].status == "needs_review"


def test_rerun_extract_rejects_confirmed_document(ctx: SimpleNamespace) -> None:
    """確定済み（confirmed）を無警告で置き換えない（REST の supersede_review と同じ）。"""
    _seed_document(ctx, run_status="confirmed")
    r = _confirm(ctx, "uploader", "rerun_extract", {"document_id": "doc_1"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "確定済み" in r.json()["message"]
    assert ctx.queue.messages == []
    assert _runs(ctx)["run_1"].status == "confirmed"


def test_rerun_extract_rejects_while_processing(ctx: SimpleNamespace) -> None:
    _seed_document(ctx, run_status="processing")
    r = _confirm(ctx, "uploader", "rerun_extract", {"document_id": "doc_1"})
    assert r.json()["ok"] is False and "処理中" in r.json()["message"]
    assert ctx.queue.messages == []


def test_rerun_extract_without_supersede_rejects_needs_review(ctx: SimpleNamespace) -> None:
    """supersede_review=false は REST 既定と同じ判定（needs_review も競合）。"""
    _seed_document(ctx, run_status="needs_review")
    r = _confirm(
        ctx, "uploader", "rerun_extract", {"document_id": "doc_1", "supersede_review": False}
    )
    assert r.status_code == 200 and r.json()["ok"] is False
    assert ctx.queue.messages == []
    assert _runs(ctx)["run_1"].status == "needs_review"


def test_rerun_extract_unknown_document_is_ok_false(ctx: SimpleNamespace) -> None:
    r = _confirm(ctx, "uploader", "rerun_extract", {"document_id": "doc_missing"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert ctx.queue.messages == []


def test_rerun_extract_unknown_schema_is_ok_false(ctx: SimpleNamespace) -> None:
    _seed_document(ctx)
    r = _confirm(ctx, "uploader", "rerun_extract", {"document_id": "doc_1", "schema_id": "sch_nope"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "スキーマ" in r.json()["message"]
    assert ctx.queue.messages == []


def test_rerun_extract_missing_document_id_is_e1003(ctx: SimpleNamespace) -> None:
    r = _confirm(ctx, "uploader", "rerun_extract", {})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "E1003"
    assert any(e["loc"] == "document_id" for e in err["details"]["errors"])


# ---- manage_rules（admin） ----


def test_manage_rules_activate_as_admin(ctx: SimpleNamespace) -> None:
    r = _confirm(ctx, "admin", "manage_rules", {"rule_id": "rul_ok", "status": "active"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["detail"] == {"rule_id": "rul_ok", "status": "active"}
    assert "有効化" in r.json()["message"]
    assert ctx.admin.get_rule("ten_1", "rul_ok").status == "active"


def test_manage_rules_retire(ctx: SimpleNamespace) -> None:
    ctx.admin.set_rule_status("ten_1", "rul_ok", "active")
    r = _confirm(ctx, "admin", "manage_rules", {"rule_id": "rul_ok", "status": "retired"})
    assert r.json()["ok"] is True and "退役" in r.json()["message"]
    assert ctx.admin.get_rule("ten_1", "rul_ok").status == "retired"


@pytest.mark.parametrize("role", ["viewer", "uploader", "reviewer"])
def test_manage_rules_denied_below_admin(ctx: SimpleNamespace, role: str) -> None:
    """PATCH /rules/{id} と同じ admin 限定。"""
    r = _confirm(ctx, role, "manage_rules", {"rule_id": "rul_ok", "status": "active"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "E5001"
    assert ctx.admin.get_rule("ten_1", "rul_ok").status == "draft"


def test_manage_rules_unvalidated_rule_is_ok_false(ctx: SimpleNamespace) -> None:
    """検証未達のルールはチャット経由でも有効化できない（PATCH /rules と同じゲート）。"""
    r = _confirm(ctx, "admin", "manage_rules", {"rule_id": "rul_bad", "status": "active"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "検証未達" in r.json()["message"]
    assert ctx.admin.get_rule("ten_1", "rul_bad").status == "draft"


def test_manage_rules_unknown_rule_is_ok_false(ctx: SimpleNamespace) -> None:
    r = _confirm(ctx, "admin", "manage_rules", {"rule_id": "rul_nope", "status": "active"})
    assert r.status_code == 200 and r.json()["ok"] is False


@pytest.mark.parametrize("status", ["rejected", "disabled", "draft", ""])
def test_manage_rules_rejects_unknown_status(ctx: SimpleNamespace, status: str) -> None:
    """語彙は active / retired のみ（旧 rejected / disabled は状態語彙に無い）。"""
    r = _confirm(ctx, "admin", "manage_rules", {"rule_id": "rul_ok", "status": status})
    assert r.status_code == 422 and r.json()["error"]["code"] == "E1003"
    assert ctx.admin.get_rule("ten_1", "rul_ok").status == "draft"


# ---- update_schema（admin。従来どおり） ----


@pytest.mark.parametrize("role", ["viewer", "uploader", "reviewer"])
def test_update_schema_denied_below_admin(ctx: SimpleNamespace, role: str) -> None:
    body = {"doc_type": "invoice", "field": {"name": "note", "label": "備考"}}
    r = _confirm(ctx, role, "update_schema", body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "E5001"
    assert ctx.admin.get_schema("ten_1", "invoice").version == 4


def test_update_schema_default_doc_type_is_invoice(ctx: SimpleNamespace) -> None:
    r = _confirm(ctx, "admin", "update_schema", {"field": {"name": "note", "label": "備考"}})
    assert r.json()["ok"] is True
    assert r.json()["detail"] == {"doc_type": "invoice", "version": 5}


def test_update_schema_missing_field_is_e1003(ctx: SimpleNamespace) -> None:
    r = _confirm(ctx, "admin", "update_schema", {"doc_type": "invoice"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "E1003"


# ---- 共通 ----


def test_unknown_action_is_e1003(ctx: SimpleNamespace) -> None:
    r = _confirm(ctx, "admin", "delete_everything", {})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E1003"


def test_confirm_requires_auth(ctx: SimpleNamespace) -> None:
    r = ctx.client.post("/v1/chat/confirm", json={"action": "rerun_extract", "params": {"document_id": "x"}})
    assert r.status_code == 403


def test_every_write_tool_has_a_role_and_params_model() -> None:
    """supervisor の書込みツールは全部 /chat/confirm で実行できる（片方だけ増えない）。"""
    assert set(WRITE_TOOL_MIN_ROLE) == set(WRITE_TOOLS)
    assert WRITE_TOOL_MIN_ROLE == {
        "rerun_extract": "uploader",
        "update_schema": "admin",
        "manage_rules": "admin",
    }


_PARAMS_MODEL: dict[str, type[Any]] = {
    "rerun_extract": dto.ChatRerunExtractParams,
    "update_schema": dto.ChatUpdateSchemaParams,
    "manage_rules": dto.ChatManageRulesParams,
}


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_write_tool_spec_matches_confirm_params(tool: str) -> None:
    """LLM に見せる引数名（TOOL_SPECS）と /chat/confirm が受ける params が一致する。

    confirm_request は「ツール引数 + action + prompt」の平坦な dict で、UI は
    action / prompt を除いた残りをそのまま返す。名前がずれると承認が必ず E1003 になる。
    """
    spec = next(t for t in TOOL_SPECS if t["name"] == tool)
    model = _PARAMS_MODEL[tool]
    spec_args = set(spec["input_schema"]["properties"]) - {"prompt"}
    assert spec_args <= set(model.model_fields), f"{tool}: {spec_args - set(model.model_fields)}"
    # LLM に必須のものは DTO でも受けられる。DTO で必須のものは LLM にも必須。
    dto_required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert dto_required <= set(spec["input_schema"]["required"])
    # ツール引数の "action" は confirm の "action"（ツール名）を上書きしてしまうので禁止
    assert "action" not in spec_args


def test_manage_rules_status_vocabulary_is_shared() -> None:
    spec = next(t for t in TOOL_SPECS if t["name"] == "manage_rules")
    literal = dto.ChatManageRulesParams.model_fields["status"].annotation
    assert set(spec["input_schema"]["properties"]["status"]["enum"]) == set(get_args(literal))
    assert set(get_args(literal)) == {"active", "retired"}


# ---- end-to-end: グラフが出した confirm_request をそのまま /chat/confirm へ ----


def _sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    ev = None
    for line in text.splitlines():
        if line.startswith("event: "):
            ev = line[len("event: ") :]
        elif line.startswith("data: ") and ev is not None:
            events.append((ev, json.loads(line[len("data: ") :])))
    return events


def _web_params(confirm: dict[str, Any]) -> dict[str, Any]:
    """web/app/chat/page.tsx approve() と同じ変換（action / prompt を除いた残りをそのまま）。"""
    return {k: v for k, v in confirm.items() if k not in ("action", "prompt")}


def _install_supervisor(ctx: SimpleNamespace, decision: dict[str, Any]) -> None:
    """決定論の provider で SupervisorChatAgent を組み、テストアプリに差し込む。"""
    pytest.importorskip("langgraph", reason="チャットグラフは runtime extra")
    from newfan_gateway.chat_graph_agent import SupervisorChatAgent
    from newfan_gateway.chat_tools import ChatTools

    provider = SimpleNamespace(
        complete=lambda **kw: SimpleNamespace(
            text=json.dumps(decision, ensure_ascii=False), input_tokens=1, output_tokens=1
        )
    )
    tools = ChatTools(repo=ctx.repo, admin=ctx.admin, queue=ctx.queue)
    ctx.client.app.state.chat_agent = SupervisorChatAgent(provider=provider, tools=tools)


@pytest.mark.parametrize(
    "decision,role,expect_detail",
    [
        (
            {
                "tool": "rerun_extract",
                "args": {"document_id": "doc_1", "schema_id": "sch_1", "prompt": "再抽出しますか？"},
                "text": "承認をお願いします。",
            },
            "uploader",
            {"document_id": "doc_1"},
        ),
        (
            {
                "tool": "update_schema",
                "args": {
                    "doc_type": "invoice",
                    "field": {"name": "note", "label": "備考", "type": "string"},
                    "prompt": "追加しますか？",
                },
                "text": "承認をお願いします。",
            },
            "admin",
            {"doc_type": "invoice", "version": 5},
        ),
        (
            {
                "tool": "manage_rules",
                "args": {"rule_id": "rul_ok", "status": "active", "prompt": "有効化しますか？"},
                "text": "承認をお願いします。",
            },
            "admin",
            {"rule_id": "rul_ok", "status": "active"},
        ),
    ],
    ids=["rerun_extract", "update_schema", "manage_rules"],
)
def test_confirm_request_from_graph_round_trips_through_endpoint(
    ctx: SimpleNamespace, decision: dict[str, Any], role: str, expect_detail: dict[str, Any]
) -> None:
    """POST /chat（supervisor）→ confirm_request → UI と同じ変換 → POST /chat/confirm。

    形がずれるとここで落ちる（以前は rerun_extract / manage_rules が E1003 になっていた）。
    """
    _seed_document(ctx)
    _install_supervisor(ctx, decision)

    r = ctx.client.post("/v1/chat", headers=auth("viewer"), json={"message": "やって"})
    assert r.status_code == 200
    evs = _sse(r.text)
    confirms = [d for t, d in evs if t == "confirm_request"]
    assert len(confirms) == 1, evs
    confirm = confirms[0]
    assert confirm["action"] == decision["tool"]
    assert confirm["prompt"] == decision["args"]["prompt"]
    assert evs[-1] == ("done", {"reason": "confirm_pending"})
    # 承認前は何も起きていない
    assert ctx.queue.messages == []
    assert ctx.admin.get_schema("ten_1", "invoice").version == 4
    assert ctx.admin.get_rule("ten_1", "rul_ok").status == "draft"

    r2 = _confirm(ctx, role, confirm["action"], _web_params(confirm))
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True, body
    for k, v in expect_detail.items():
        assert body["detail"][k] == v


def test_confirm_request_from_graph_is_role_gated(ctx: SimpleNamespace) -> None:
    """viewer は提案（/chat）は受け取れるが、承認実行は非チャット API と同じ権限が要る。"""
    _seed_document(ctx)
    _install_supervisor(
        ctx,
        {"tool": "rerun_extract", "args": {"document_id": "doc_1", "prompt": "?"}, "text": ""},
    )
    evs = _sse(ctx.client.post("/v1/chat", headers=auth("viewer"), json={"message": "再抽出"}).text)
    confirm = [d for t, d in evs if t == "confirm_request"][0]
    r = _confirm(ctx, "viewer", confirm["action"], _web_params(confirm))
    assert r.status_code == 403
    assert ctx.queue.messages == []
