"""LangGraph チェックポイント用 serde（msgpack 型許可リスト, §4.4）。

チェックポイントへ載る Pydantic モデル/Enum（ExtractionState の spans/layout/fields/
tables/review_items 等）を明示登録する。これにより:
  - 「未登録型の逆シリアライズ」警告（将来版でブロック）を解消する。
  - 許可リスト外の任意型の逆シリアライズを禁止し、チェックポイント復元時の
    任意コード実行を防ぐ（LANGGRAPH_STRICT_MSGPACK 相当の安全性）。

newfan_schemas 配下を走査して全モデル/Enum を自動収集するため、schema 追加時も
追従する。langgraph は optional-dependency（graph extra）。
"""

from __future__ import annotations

import enum
import importlib
import inspect
import pkgutil
from typing import Any

from pydantic import BaseModel


def schema_types() -> list[type]:
    """newfan_schemas 配下の Pydantic モデル/Enum を全収集する。"""
    import newfan_schemas

    found: dict[tuple[str, str], type] = {}
    for mod in pkgutil.walk_packages(
        newfan_schemas.__path__, newfan_schemas.__name__ + "."
    ):
        module = importlib.import_module(mod.name)
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if not obj.__module__.startswith("newfan_schemas"):
                continue  # pydantic.BaseModel / enum.Enum など import されたものを除外
            if issubclass(obj, (BaseModel, enum.Enum)):
                found[(obj.__module__, obj.__name__)] = obj
    return list(found.values())


def newfan_serde() -> Any:
    """schema 型を許可した JsonPlusSerializer を返す（checkpointer の serde に注入）。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=schema_types())
