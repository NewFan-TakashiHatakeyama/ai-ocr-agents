"""テスト共有ヘルパー（一意名にして conftest 名の衝突を避ける）。"""

from __future__ import annotations

import jwt

from newfan_ingest.rasterize import RasterPage

TEST_SECRET = "test-secret-0123456789-abcdefghijklmnop"  # >=32 bytes (HS256)
PDF = b"%PDF-1.7\nfake-pdf-content"


class FakeRasterizer:
    def rasterize(self, content: bytes, kind: str) -> list[RasterPage]:
        return [RasterPage(page_no=1, width=1000, height=1400, png_bytes=b"\x89PNG-page-1")]


def make_token(role: str = "uploader", tenant: str = "ten_1", sub: str = "u1") -> str:
    return jwt.encode(
        {"sub": sub, "tenant_id": tenant, "role": role}, TEST_SECRET, algorithm="HS256"
    )


def auth(role: str = "uploader") -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(role)}"}
