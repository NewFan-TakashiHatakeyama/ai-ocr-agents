"""条件分岐の式（docs/design/16-agent-workflow.md §4.3）。

任意の式は評価しない。eval は論外として、汎用式言語を入れると lint も
テストもできなくなる。以下の限定文法だけを解釈する:

    <expr>    := <operand> <op> <literal>
    <operand> := run.status | run.confidence | doc.doc_type
               | field['<名前>'].value | field['<名前>'].confidence
    <op>      := == | != | > | >= | < | <=
    <literal> := 数値 | 'シングルクォート文字列'

複合条件（and/or）は MVP では持たない（未決事項 Q2）。必要になれば
この文法へ後方互換で足せる（保存済みの単項式はそのままパースできる）。

型は **parse 時に** 検査する。`run.status > 5` のような型違いは保存時
（モデル検証）で弾かれ、実行時まで生き残らない。
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional, Union

OperandKind = Literal[
    "run.status", "run.confidence", "doc.doc_type", "field.value", "field.confidence"
]


class ExprError(ValueError):
    """式が文法または型規則に合わない。保存時（モデル検証）に投げる。"""


_OPERAND_FIELD = re.compile(r"^field\['([^'\]]+)'\]\.(value|confidence)")
_OPERAND_PLAIN = re.compile(r"^(run\.status|run\.confidence|doc\.doc_type)")
_OP = re.compile(r"^(==|!=|>=|<=|>|<)")  # >= <= を先に（> < が先だと 2 文字目が残る）
_LIT_STR = re.compile(r"^'([^']*)'")
_LIT_NUM = re.compile(r"^-?\d+(?:\.\d+)?")

# 数値として比較できる operand。それ以外は文字列
_NUMERIC_KINDS: frozenset[str] = frozenset({"run.confidence", "field.confidence"})
_ORDERING_OPS: frozenset[str] = frozenset({">", ">=", "<", "<="})

_OPS: Mapping[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


@dataclass(frozen=True)
class Condition:
    """parse 済みの単項条件。graph_json には元の文字列（source）のまま保存する。"""

    kind: OperandKind
    op: str
    literal: Union[str, float]
    field_name: Optional[str] = None
    source: str = ""


def parse_expr(text: str) -> Condition:
    """式を parse し、型規則まで検査する。不正は ExprError。

    - 順序比較（> >= < <=）は数値 operand × 数値リテラルのみ
    - 等値比較（== !=）はリテラルの型が operand の型と一致すること
    """
    s = text.strip()
    if not s:
        raise ExprError("条件式が空です")

    rest = s
    kind: OperandKind
    field_name: Optional[str] = None
    if m := _OPERAND_FIELD.match(rest):
        field_name = m.group(1)
        kind = "field.value" if m.group(2) == "value" else "field.confidence"
        rest = rest[m.end() :]
    elif m := _OPERAND_PLAIN.match(rest):
        kind = m.group(1)  # type: ignore[assignment]
        rest = rest[m.end() :]
    else:
        raise ExprError(
            f"左辺が不正です: {s!r}"
            "（run.status / run.confidence / doc.doc_type / field['名前'].value / "
            "field['名前'].confidence のいずれか）"
        )

    rest = rest.lstrip()
    if not (m := _OP.match(rest)):
        raise ExprError(f"比較演算子が不正です: {rest!r}（== != > >= < <= のいずれか）")
    op = m.group(1)
    rest = rest[m.end() :].lstrip()

    literal: Union[str, float]
    if m := _LIT_STR.match(rest):
        literal = m.group(1)
    elif m := _LIT_NUM.match(rest):
        literal = float(m.group(0))
    else:
        raise ExprError(f"右辺が不正です: {rest!r}（数値 か 'シングルクォート文字列'）")
    rest = rest[m.end() :].strip()
    if rest:
        # and/or を書かれた場合もここに落ちる。黙って無視すると意図と違う条件で
        # 分岐してしまうため、必ずエラーにする。
        raise ExprError(f"式の末尾に解釈できない残りがあります: {rest!r}（複合条件は未対応）")

    numeric = kind in _NUMERIC_KINDS
    if op in _ORDERING_OPS:
        if not numeric:
            raise ExprError(f"{kind} は文字列なので順序比較（{op}）はできません: {s!r}")
        if not isinstance(literal, float):
            raise ExprError(f"順序比較の右辺は数値にしてください: {s!r}")
    else:  # == / !=
        if numeric and not isinstance(literal, float):
            raise ExprError(f"{kind} は数値なので右辺も数値にしてください: {s!r}")
        if not numeric and not isinstance(literal, str):
            raise ExprError(f"{kind} は文字列なので右辺は 'クォート' で囲んでください: {s!r}")

    return Condition(kind=kind, op=op, literal=literal, field_name=field_name, source=s)


@dataclass(frozen=True)
class FieldView:
    """評価時に見せるフィールドの断面（抽出結果から組む）。"""

    value: Optional[str] = None
    confidence: Optional[float] = None


@dataclass(frozen=True)
class EvalContext:
    run_status: Optional[str] = None
    run_confidence: Optional[float] = None
    doc_type: Optional[str] = None
    fields: Mapping[str, FieldView] = field(default_factory=dict)


def _resolve(cond: Condition, ctx: EvalContext) -> Union[str, float, None]:
    if cond.kind == "run.status":
        return ctx.run_status
    if cond.kind == "run.confidence":
        return ctx.run_confidence
    if cond.kind == "doc.doc_type":
        return ctx.doc_type
    fv = ctx.fields.get(cond.field_name or "")
    if fv is None:
        return None
    return fv.value if cond.kind == "field.value" else fv.confidence


def evaluate(cond: Condition, ctx: EvalContext) -> bool:
    """値が取れない（None / フィールド不在）場合はどの条件にも一致しない（False）。

    != でも True にしない。None != 'x' を True にすると、抽出に失敗した帳票が
    「正常」の経路へ流れてしまう。取れなかった帳票は必ず else へ落とす。
    """
    value = _resolve(cond, ctx)
    if value is None:
        return False
    return bool(_OPS[cond.op](value, cond.literal))
