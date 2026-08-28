"""ObjectStore.delete_prefix（§2.3 帳票単位の実体削除）。

ここでの主な関心は 2 つ:

- **消しすぎない**: prefix の組み立てをどこかで間違えると、1 帳票を消すつもりで
  テナント丸ごと・バケット丸ごとが消える。空文字や "ten_1/" は例外で止める。
- **消し残さない**: production の S3 はバージョニング有効。VersionId を付けない
  DeleteObject は削除マーカーを置くだけで、バイトはバケットに残る（＝UI の
  「元に戻せません」が嘘になる）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from newfan_ingest.storage import LocalObjectStore, S3ObjectStore


# --- prefix ガード（両実装で同じ） ---


@pytest.mark.parametrize(
    "bad",
    [
        "", "/", "ten_1/", "ten_1", "ten_1/doc_1", "/ten_1/doc_1/", "ten_1/doc_1/pages/",
        # パストラバーサル。正規表現の文字クラスが "." を許すため、素朴に書くと
        # ".." が 1 セグメントとして通り "a/../" が保管ルート丸ごとの rmtree になる。
        "../../", "a/../", "./../", "../a/", "..../..../", "ten_1/../",
    ],
)
def test_delete_prefix_rejects_non_document_prefix(tmp_path: Path, bad: str) -> None:
    store = LocalObjectStore(tmp_path)
    with pytest.raises(ValueError):
        store.delete_prefix(bad)


def test_local_delete_prefix_never_escapes_storage_root(tmp_path: Path) -> None:
    """保管ルートの外は何があっても消さない（正規表現ガードを抜けても実パスで止める）。"""
    root = tmp_path / "storage_root"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me")
    store = LocalObjectStore(root)
    store.put("ten_1/doc_1/original.pdf", b"pdf", "application/pdf")

    for bad in ("../../", "a/../", "ten_1/../"):
        with pytest.raises(ValueError):
            store.delete_prefix(bad)

    assert (victim / "important.txt").read_text() == "keep me"
    assert (root / "ten_1" / "doc_1" / "original.pdf").exists()


# --- Local ---


def test_local_delete_prefix_removes_and_counts(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put("ten_1/doc_1/original.pdf", b"pdf", "application/pdf")
    store.put("ten_1/doc_1/pages/1.png", b"png1", "image/png")
    store.put("ten_1/doc_1/pages/2.png", b"png2", "image/png")
    # 巻き添えにしてはいけない隣人
    store.put("ten_1/doc_2/original.pdf", b"other", "application/pdf")

    assert store.delete_prefix("ten_1/doc_1/") == 3
    assert not (tmp_path / "ten_1" / "doc_1").exists()
    assert (tmp_path / "ten_1" / "doc_2" / "original.pdf").read_bytes() == b"other"


def test_local_delete_prefix_missing_dir_is_zero(tmp_path: Path) -> None:
    """2 回目の削除や、実体が既に無い帳票でも例外にしない（冪等）。"""
    assert LocalObjectStore(tmp_path).delete_prefix("ten_1/doc_gone/") == 0


# --- S3 ---


class _FakeS3:
    """list_object_versions / delete_objects だけを持つ最小の偽 client。"""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.deleted: list[dict[str, str]] = []
        self.delete_calls = 0
        self.list_kwargs: list[dict[str, Any]] = []

    def list_object_versions(self, **kw: Any) -> dict[str, Any]:
        self.list_kwargs.append(kw)
        return self._pages[len(self.list_kwargs) - 1]

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self.delete_calls += 1
        self.deleted.extend(Delete["Objects"])
        return {"Deleted": Delete["Objects"]}


def _store(client: Any) -> S3ObjectStore:
    return S3ObjectStore("bkt", client=client)


def test_s3_delete_prefix_removes_versions_and_delete_markers() -> None:
    """バージョニング有効なバケットでは、旧版と削除マーカーまで消さないと
    バイトが残る（＝「元に戻せません」が嘘になる）。"""
    cli = _FakeS3(
        [
            {
                "Versions": [
                    {"Key": "ten_1/doc_1/original.pdf", "VersionId": "v1"},
                    {"Key": "ten_1/doc_1/original.pdf", "VersionId": "v2"},
                ],
                "DeleteMarkers": [{"Key": "ten_1/doc_1/pages/1.png", "VersionId": "dm1"}],
                "IsTruncated": False,
            }
        ]
    )
    assert _store(cli).delete_prefix("ten_1/doc_1/") == 3
    assert {d["VersionId"] for d in cli.deleted} == {"v1", "v2", "dm1"}


def test_s3_delete_prefix_follows_pagination() -> None:
    cli = _FakeS3(
        [
            {
                "Versions": [{"Key": "ten_1/doc_1/a", "VersionId": "v1"}],
                "IsTruncated": True,
                "NextKeyMarker": "ten_1/doc_1/a",
                "NextVersionIdMarker": "v1",
            },
            {"Versions": [{"Key": "ten_1/doc_1/b", "VersionId": "v2"}], "IsTruncated": False},
        ]
    )
    assert _store(cli).delete_prefix("ten_1/doc_1/") == 2
    assert cli.list_kwargs[1]["KeyMarker"] == "ten_1/doc_1/a"
    assert cli.list_kwargs[1]["VersionIdMarker"] == "v1"


def test_s3_delete_prefix_skips_empty_page() -> None:
    """空の Objects で delete_objects を呼ぶと ParamValidationError になる。"""
    cli = _FakeS3([{"IsTruncated": False}])
    assert _store(cli).delete_prefix("ten_1/doc_1/") == 0
    assert cli.delete_calls == 0


def test_s3_delete_prefix_raises_on_partial_errors() -> None:
    """部分成功を成功として返すと「消えたはず」が黙って崩れる。

    呼び出し側は例外を受けて DB を消さずに 500 を返し、再試行で前進する。
    """

    class _PartialFail(_FakeS3):
        def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
            return {"Deleted": [], "Errors": [{"Key": "ten_1/doc_1/a", "Code": "AccessDenied"}]}

    cli = _PartialFail(
        [{"Versions": [{"Key": "ten_1/doc_1/a", "VersionId": "v1"}], "IsTruncated": False}]
    )
    with pytest.raises(RuntimeError, match="AccessDenied"):
        _store(cli).delete_prefix("ten_1/doc_1/")
