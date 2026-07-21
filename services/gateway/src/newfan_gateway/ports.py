"""外部サービスの port（Protocol）。ingest / orchestrator。"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from newfan_ingest import IngestResult, UploadInput


class Ingestor(Protocol):
    def ingest(self, upload: UploadInput, *, source_hint: str = "unknown") -> IngestResult:
        """検証→保存→ページ分割→前処理（§5.1/§5.2）。newfan_ingest.IngestService が実装。"""
        ...


class OrchestratorClient(Protocol):
    def resume(
        self,
        run_id: str,
        tenant_id: str,
        feedback: Optional[dict[str, Any]],
        *,
        notify: Optional[dict[str, Any]] = None,
    ) -> None:
        """HITL 確定でグラフを resume（§4.4）。実体は再開ジョブ発行（Web 内で長時間実行しない, §4.4）。

        notify はワークフロー起点の Run（extraction_runs.options.workflow_notify）の
        中継。確定フロー完了時にワーカーが confirm_done をそこへ積む（§16 §8 / P5）。
        """
        ...


class FakeOrchestratorClient:
    def __init__(self) -> None:
        self.resumed: list[tuple[str, str, Optional[dict[str, Any]]]] = []
        self.notified: list[Optional[dict[str, Any]]] = []  # resumed と同順

    def resume(
        self,
        run_id: str,
        tenant_id: str,
        feedback: Optional[dict[str, Any]],
        *,
        notify: Optional[dict[str, Any]] = None,
    ) -> None:
        self.resumed.append((run_id, tenant_id, feedback))
        self.notified.append(notify)


class SecretStore(Protocol):
    """秘密の保管（Secrets Manager, §16.5 / P6）。DB には参照（ARN）だけを置く。"""

    def create(self, name: str, value: str) -> str:
        """保存して参照（ARN）を返す。同名が既にあれば新しい値で更新する。"""
        ...

    def get(self, ref: str) -> str: ...


class FakeSecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def create(self, name: str, value: str) -> str:
        ref = f"arn:fake:{name}"
        self.values[ref] = value
        return ref

    def get(self, ref: str) -> str:
        return self.values[ref]
