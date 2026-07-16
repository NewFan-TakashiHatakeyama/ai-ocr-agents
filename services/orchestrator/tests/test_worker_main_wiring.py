"""worker_main の本番配線（§2.1 / §5.8）。

MemoryService に adapter/bundle を渡し忘れていたため、learn が「_adapter is None」で
即 return し、修正が何件たまってもルール抽出（§5.8.4）が一度も走らなかった。
実 AWS で tenant_memories は増えるのに tenant_rules が 0 件のままだったのはこれが原因。
本番配線を通すテストが無かったため、誰も気付けなかった。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph", reason="worker_main は runtime extra")

from newfan_orchestrator import worker_main  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """E5Embedder の実ロードを避ける（本物は 60 秒以上かかり単体テストに向かない）。

    _memory は E5Embedder が落ちたら HashingEmbedder へ degrade する。ここでは
    その degrade 経路に乗せて、検証対象（adapter/bundle と repo の選択）だけを見る。
    """
    import newfan_memory.e5_embedder as e5

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("テストでは実モデルをロードしない")

    monkeypatch.setattr(e5, "E5Embedder", _boom)


def test_memory_gets_adapter_and_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """_memory が adapter/bundle を MemoryService へ渡すこと。

    渡っていないと learn がルール抽出を一切トリガしない（サイレントに学習が止まる）。
    """
    captured: dict[str, Any] = {}

    class _FakeMemoryService:
        def __init__(self, embedder: Any, repo: Any, **kw: Any) -> None:
            captured.update(embedder=embedder, repo=repo, **kw)

    monkeypatch.setattr(worker_main, "MemoryService", _FakeMemoryService)
    monkeypatch.delenv("DATABASE_URL", raising=False)  # InMemory へ degrade させる

    adapter, bundle = object(), object()
    worker_main._memory(adapter, bundle)  # type: ignore[arg-type]

    assert captured["adapter"] is adapter, "adapter が渡っていない（ルール抽出が動かない）"
    assert captured["bundle"] is bundle, "bundle が渡っていない（ルール抽出が動かない）"


def test_memory_degrades_to_inmemory_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DATABASE_URL が無ければ InMemory リポジトリへ落ちる（§5.8.3 の degrade）。"""
    captured: dict[str, Any] = {}

    class _FakeMemoryService:
        def __init__(self, embedder: Any, repo: Any, **kw: Any) -> None:
            captured.update(repo=repo)

    monkeypatch.setattr(worker_main, "MemoryService", _FakeMemoryService)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    worker_main._memory(object(), object())  # type: ignore[arg-type]

    from newfan_memory import InMemoryMemoryRepository

    assert isinstance(captured["repo"], InMemoryMemoryRepository)
