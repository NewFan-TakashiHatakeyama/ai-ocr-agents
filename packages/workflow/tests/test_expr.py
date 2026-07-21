"""条件式の文法・型規則・評価（§4.3）。

式は分岐の行き先を決める。誤った式が「黙って False」や「黙って部分解釈」に
なると、帳票が意図しない経路（最悪、基幹 DB への書込み）へ流れる。
parse 時に落とすことと、値が無いときの挙動を固定する。
"""

from __future__ import annotations

import pytest

from newfan_workflow import EvalContext, ExprError, FieldView, evaluate, parse_expr


def _ctx(**kw: object) -> EvalContext:
    return EvalContext(**kw)  # type: ignore[arg-type]


# ---------- parse + evaluate（正常系） ----------


@pytest.mark.parametrize(
    ("expr", "ctx", "expected"),
    [
        ("run.status == 'needs_review'", _ctx(run_status="needs_review"), True),
        ("run.status == 'needs_review'", _ctx(run_status="confirmed"), False),
        ("run.status != 'failed'", _ctx(run_status="confirmed"), True),
        ("run.confidence >= 0.8", _ctx(run_confidence=0.85), True),
        ("run.confidence >= 0.8", _ctx(run_confidence=0.79), False),
        ("run.confidence < 1", _ctx(run_confidence=0.5), True),
        ("doc.doc_type == 'invoice'", _ctx(doc_type="invoice"), True),
        (
            "field['合計金額'].value == '7003'",
            _ctx(fields={"合計金額": FieldView(value="7003")}),
            True,
        ),
        (
            "field['合計金額'].confidence > 0.9",
            _ctx(fields={"合計金額": FieldView(confidence=0.95)}),
            True,
        ),
        (
            "field['合計金額'].confidence > 0.9",
            _ctx(fields={"合計金額": FieldView(confidence=0.7)}),
            False,
        ),
        # 空白の揺れを許す
        ("run.status=='needs_review'", _ctx(run_status="needs_review"), True),
        ("  run.confidence   >=   0.8  ", _ctx(run_confidence=0.9), True),
        # 負数
        ("run.confidence > -1", _ctx(run_confidence=0.0), True),
    ],
)
def test_正しい式はparseでき評価できる(expr: str, ctx: EvalContext, expected: bool) -> None:
    assert evaluate(parse_expr(expr), ctx) is expected


# ---------- 値が無いとき ----------


def test_フィールドが無ければFalse() -> None:
    cond = parse_expr("field['備考'].value == '至急'")
    assert evaluate(cond, _ctx(fields={})) is False


def test_値が無ければ不等号でもFalse() -> None:
    # None != 'x' を True にすると、抽出に失敗した帳票が「正常」の経路へ流れる。
    # 取れなかった帳票は必ず else へ落とす、を固定する。
    cond = parse_expr("field['備考'].value != '至急'")
    assert evaluate(cond, _ctx(fields={})) is False
    assert evaluate(cond, _ctx(fields={"備考": FieldView(value=None)})) is False


def test_run_statusが未設定ならFalse() -> None:
    assert evaluate(parse_expr("run.status != 'failed'"), _ctx()) is False


# ---------- 型規則（parse 時に落とす） ----------


@pytest.mark.parametrize(
    "expr",
    [
        "run.status > 5",  # 文字列 operand に順序比較
        "run.status > 'a'",  # 同上（リテラルが文字列でも不可）
        "field['x'].value >= '10'",  # value は文字列
        "run.status == 5",  # 文字列 operand に数値リテラル
        "run.confidence == 'high'",  # 数値 operand に文字列リテラル
        "run.confidence >= 'x'",
    ],
)
def test_型が合わない式はparseで落ちる(expr: str) -> None:
    with pytest.raises(ExprError):
        parse_expr(expr)


# ---------- 文法違反 ----------


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "   ",
        "1 == 1",  # 左辺が operand でない
        "status == 'x'",  # 未知の operand
        "run.status = 'x'",  # 代入演算子
        "run.status == needs_review",  # クォート無し
        'run.status == "needs_review"',  # ダブルクォートは不可（文法を 1 つに絞る）
        "field[total].value == 'x'",  # フィールド名のクォート無し
        "run.status == 'a' or run.status == 'b'",  # 複合条件（未対応。黙って部分解釈しない）
        "run.confidence >= 0.8 and doc.doc_type == 'invoice'",
        "run.status == 'a' garbage",  # 末尾のゴミ
    ],
)
def test_文法に合わない式はparseで落ちる(expr: str) -> None:
    with pytest.raises(ExprError):
        parse_expr(expr)


def test_エラーは何が悪いか分かる文面を持つ() -> None:
    with pytest.raises(ExprError, match="複合条件"):
        parse_expr("run.status == 'a' or run.status == 'b'")
    with pytest.raises(ExprError, match="順序比較"):
        parse_expr("doc.doc_type > 'a'")


def test_sourceに元の式が残る() -> None:
    # graph_json には文字列のまま保存する。UI が表示に使う。
    cond = parse_expr("  run.confidence >= 0.8 ")
    assert cond.source == "run.confidence >= 0.8"
