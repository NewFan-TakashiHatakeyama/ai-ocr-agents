"""ワークフロー定義・構成 lint・条件式（§16 / docs/design/16-agent-workflow.md）。

純ロジック（DB も HTTP も持たない）。gateway は保存時の検証と lint に、
orchestrator（workflow-runner）は実行時の解釈に、それぞれこのパッケージを使う。
定義を 1 箇所に置くのは、保存時に通った graph_json が実行時に読めない、という
乖離を構造的に無くすため。
"""

from newfan_workflow.classify import (
    ClassifyCandidate,
    ClassifyOutcome,
    build_candidate,
    classify_text,
    synonyms_for,
)
from newfan_workflow.expr import (
    Condition,
    EvalContext,
    ExprError,
    FieldView,
    evaluate,
    parse_expr,
)
from newfan_workflow.lint import Finding, Severity, has_errors, lint
from newfan_workflow.models import (
    NODE_CONFIG_MODELS,
    Edge,
    WorkflowGraph,
    WorkflowNode,
    catalog,
    is_sink,
    is_trigger,
)

__all__ = [
    "ClassifyCandidate",
    "ClassifyOutcome",
    "Condition",
    "Edge",
    "EvalContext",
    "ExprError",
    "FieldView",
    "Finding",
    "NODE_CONFIG_MODELS",
    "build_candidate",
    "classify_text",
    "synonyms_for",
    "Severity",
    "WorkflowGraph",
    "WorkflowNode",
    "catalog",
    "evaluate",
    "has_errors",
    "is_sink",
    "is_trigger",
    "lint",
    "parse_expr",
]
