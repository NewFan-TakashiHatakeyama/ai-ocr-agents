"""傾き補正（DeskewPreprocessor, ADR-0002 追記）と INGEST_PREPROCESS の選択。

合成ページ（白地に黒の横線＝文字行）を傾けて、推定角が元に戻す向き・大きさになるかを見る。
Pillow が無い環境（runtime extra 未導入）では skip。
"""

from __future__ import annotations

import io
import logging

import pytest

from newfan_ingest.preprocess import (
    DeskewPreprocessor,
    NoopPreprocessor,
    preprocessor_from_env,
)
from newfan_ingest.rasterize import RasterPage

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402


def _text_page(skew_deg: float = 0.0, size: tuple[int, int] = (1000, 1400)) -> bytes:
    """白地に黒の横線を等間隔に引いた「文字行」のページ。skew_deg だけ（反時計回りに）傾ける。"""
    im = Image.new("L", size, 255)
    d = ImageDraw.Draw(im)
    for y in range(150, size[1] - 150, 40):
        d.rectangle((100, y, size[0] - 100, y + 12), fill=0)
    if skew_deg:
        im = im.rotate(skew_deg, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _page(png: bytes, page_no: int = 1) -> RasterPage:
    with Image.open(io.BytesIO(png)) as im:
        w, h = im.size
    return RasterPage(page_no=page_no, width=w, height=h, png_bytes=png)


@pytest.mark.parametrize("skew", [2.0, -1.5, 4.0])
def test_estimate_angle_は傾きを打ち消す向きで返す(skew: float) -> None:
    angle, detail = DeskewPreprocessor().estimate_angle(_text_page(skew))
    assert abs(angle + skew) <= 0.3, (angle, detail)
    assert detail["applied"] is True and detail["gain"] > 1.05


def test_preprocess_は傾いたページを回して寸法を変えない() -> None:
    page = _page(_text_page(2.0))
    out = DeskewPreprocessor().preprocess(page)
    assert (out.width, out.height) == (page.width, page.height)
    assert out.png_bytes != page.png_bytes
    assert abs(float(out.preproc["angle"]) + 2.0) <= 0.3  # type: ignore[arg-type]
    assert out.preproc["unwarp"] is False and out.preproc["scale"] == 1.0
    # 補正後はほぼ水平（再推定で 0 に落ちる）
    again, detail = DeskewPreprocessor().estimate_angle(out.png_bytes)
    assert again == 0.0, detail


def test_preprocess_は整ったページを触らない() -> None:
    page = _page(_text_page(0.0))
    out = DeskewPreprocessor().preprocess(page)
    assert out.png_bytes is page.png_bytes  # 再エンコードしない
    assert out.preproc["angle"] == 0
    assert out.preproc["deskew"]["applied"] is False  # type: ignore[index]


def test_preprocess_は山の無い画像を補正しない() -> None:
    # 白紙: どの角度でも射影は平ら → gain が閾値に届かない → 無補正
    im = Image.new("L", (800, 1100), 255)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    page = _page(buf.getvalue())
    out = DeskewPreprocessor().preprocess(page)
    assert out.png_bytes is page.png_bytes and out.preproc["angle"] == 0


def test_preprocess_は壊れた画像でも取込を止めない(caplog: pytest.LogCaptureFixture) -> None:
    page = RasterPage(page_no=1, width=10, height=10, png_bytes=b"not a png")
    with caplog.at_level(logging.WARNING):
        out = DeskewPreprocessor().preprocess(page)
    assert out.png_bytes is page.png_bytes and out.preproc["angle"] == 0
    assert out.preproc["deskew"]["applied"] is False  # type: ignore[index]
    assert "傾き推定に失敗" in caplog.text


def test_preprocessor_from_env() -> None:
    assert isinstance(preprocessor_from_env({}), NoopPreprocessor)
    assert isinstance(preprocessor_from_env({"INGEST_PREPROCESS": "none"}), NoopPreprocessor)
    assert isinstance(preprocessor_from_env({"INGEST_PREPROCESS": "deskew"}), DeskewPreprocessor)
    assert isinstance(preprocessor_from_env({"INGEST_PREPROCESS": " Deskew "}), DeskewPreprocessor)
    assert isinstance(preprocessor_from_env({"INGEST_PREPROCESS": "bogus"}), NoopPreprocessor)


def test_deskew_measure_は推定だけして画像を回さない() -> None:
    page = _page(_text_page(2.0))
    out = DeskewPreprocessor(apply=False).preprocess(page)
    assert out.png_bytes is page.png_bytes  # 回さない・再エンコードしない
    assert (out.width, out.height) == (page.width, page.height)
    assert out.preproc["angle"] == 0
    d = out.preproc["deskew"]
    assert d["measure_only"] is True and d["applied"] is False and d["would_apply"] is True  # type: ignore[index]
    assert abs(float(d["estimated"]) + 2.0) <= 0.3  # type: ignore[index,arg-type]


def test_deskew_measure_は整ったページで_would_apply_が偽() -> None:
    out = DeskewPreprocessor(apply=False).preprocess(_page(_text_page(0.0)))
    d = out.preproc["deskew"]
    assert d["would_apply"] is False and d["applied"] is False  # type: ignore[index]


def test_preprocessor_from_env_deskew_measure() -> None:
    p = preprocessor_from_env({"INGEST_PREPROCESS": "deskew_measure"})
    assert isinstance(p, DeskewPreprocessor)
    out = p.preprocess(_page(_text_page(2.0)))
    assert out.preproc["angle"] == 0 and out.preproc["deskew"]["measure_only"] is True  # type: ignore[index]
