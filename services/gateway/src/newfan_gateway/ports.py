"""外部サービスの port（Protocol）。ingest / orchestrator。"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from newfan_ingest import IngestResult, UploadInput


class Ingestor(Protocol):
    def ingest(self, upload: UploadInput, *, source_hint: str = "unknown") -> IngestResult:
        """検証→保存→ページ分割→前処理（§5.1/§5.2）。newfan_ingest.IngestService が実装。"""
        ...


class OrchestratorClient(Protocol):
    def resume(self, run_id: str, feedback: Optional[dict[str, Any]]) -> None:
        """HITL 確定でグラフを resume（§4.4）。実体は再開ジョブ発行（Web 内で長時間実行しない, §4.4）。"""
        ...


class FakeOrchestratorClient:
    def __init__(self) -> None:
        self.resumed: list[tuple[str, Optional[dict[str, Any]]]] = []

    def resume(self, run_id: str, feedback: Optional[dict[str, Any]]) -> None:
        self.resumed.append((run_id, feedback))
