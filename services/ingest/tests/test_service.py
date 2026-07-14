from pathlib import Path

import pytest

from newfan_ingest import IngestService, UploadInput
from newfan_ingest.errors import IngestError
from newfan_ingest.rasterize import RasterPage
from newfan_ingest.storage import LocalObjectStore

PDF = b"%PDF-1.7\nfake"


class FakeRasterizer:
    def __init__(self, n_pages: int) -> None:
        self._n = n_pages

    def rasterize(self, content: bytes, kind: str) -> list[RasterPage]:
        return [
            RasterPage(page_no=i + 1, width=1000, height=1400, png_bytes=b"\x89PNGpage%d" % i)
            for i in range(self._n)
        ]


def test_ingest_splits_and_stores(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    svc = IngestService(store, FakeRasterizer(3))
    upload = UploadInput(
        tenant_id="ten_1", document_id="doc_1", filename="invoice.pdf", content=PDF
    )

    result = svc.ingest(upload)

    assert result.page_count == 3
    assert result.mime_type == "application/pdf"
    assert [p.page_no for p in result.pages] == [1, 2, 3]
    # 前処理メタ（座標系の正）が付与される
    assert result.pages[0].preproc == {"angle": 0, "unwarp": False, "scale": 1.0}
    # 原本 + 3 ページが保存されている
    assert (tmp_path / "ten_1/doc_1/original.pdf").exists()
    assert (tmp_path / "ten_1/doc_1/pages/1.png").exists()
    assert (tmp_path / "ten_1/doc_1/pages/3.png").exists()


def test_ingest_page_limit(tmp_path: Path) -> None:
    from newfan_ingest.validation import IngestLimits

    store = LocalObjectStore(tmp_path)
    svc = IngestService(store, FakeRasterizer(5), limits=IngestLimits(max_pages=2))
    upload = UploadInput(
        tenant_id="ten_1", document_id="doc_2", filename="big.pdf", content=PDF
    )
    with pytest.raises(IngestError) as exc:
        svc.ingest(upload)
    assert exc.value.code == "E1002"


def test_office_not_implemented(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    svc = IngestService(store, FakeRasterizer(1))
    upload = UploadInput(
        tenant_id="ten_1",
        document_id="doc_3",
        filename="report.docx",
        content=b"PK\x03\x04zip",
    )
    with pytest.raises(IngestError) as exc:
        svc.ingest(upload)
    assert exc.value.code == "E1003"
