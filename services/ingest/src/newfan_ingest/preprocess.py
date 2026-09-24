"""前処理（§5.2, DD-01/ADR-0002）。

ADR-0002 の決定により、向き補正・アンワープは ingest 側で行い、その PNG を
「座標系の正」とする。structure/ocr サービングは前処理オフで呼ぶ。

判定方針（§5.2）:
- スキャナ由来（EXIF なし・歪み小）: unwarp=False
- スマホ撮影疑い（EXIF あり or 台形歪み検知）: unwarp=True

MVP 初期は orientation のみでも可（unwarp は段階導入）。実装は PaddleOCR の
doc_preprocessor パイプライン単体呼び出し、または軽量な自前回転を差し込む。

【2026-09-13 追記】軽量な自前回転として ``DeskewPreprocessor``（射影プロファイルによる
傾き補正、Pillow のみ）を入れた。``INGEST_PREPROCESS=deskew`` で有効（既定は none）。
90°/180° の向き補正・アンワープは引き続き未実装。
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

from newfan_ingest.rasterize import RasterPage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreprocessedPage:
    page_no: int
    width: int
    height: int
    png_bytes: bytes
    # pages.preproc に保存: {"angle": 0, "unwarp": false, "scale": 1.0}
    preproc: dict[str, object] = field(default_factory=dict)


class Preprocessor(Protocol):
    def preprocess(self, page: RasterPage, *, source_hint: str) -> PreprocessedPage:
        """1 ページを前処理し、前処理後 PNG とメタを返す。source_hint: scanner|photo|unknown。"""
        ...


class NoopPreprocessor:
    """前処理なし（スキャナ由来・整った画像向け。MVP フォールバック）。"""

    def preprocess(self, page: RasterPage, *, source_hint: str = "unknown") -> PreprocessedPage:
        return PreprocessedPage(
            page_no=page.page_no,
            width=page.width,
            height=page.height,
            png_bytes=page.png_bytes,
            preproc={"angle": 0, "unwarp": False, "scale": 1.0},
        )


def _frange(start: float, stop: float, step: float) -> list[float]:
    out: list[float] = []
    n = int(round((stop - start) / step))
    for i in range(n + 1):
        out.append(round(start + i * step, 6))
    return out


class DeskewPreprocessor:
    """射影プロファイルによる傾き補正（DD-01 の「向き補正」の最小実装。Pillow のみ）。

    ±``max_angle`` 度の範囲を粗く（``coarse_step``）→細かく（``fine_step``）回し、
    **行方向の射影（各行の平均濃度）の分散が最大**になる回転角を採る。文字行が水平に
    揃うほど「文字のある行」と「行間」の濃淡差が大きくなることを使う（射影プロファイル法）。
    判定は幅 ``work_width`` px に縮めたグレースケール（白黒反転。回転で入る余白＝0 が射影を
    汚さない）で行い、補正は元解像度で行う（bicubic、余白は白）。

    - 最良角の分散が 0 度の分散の ``min_gain`` 倍に届かない（山が無い＝白紙・写真・罫線
      だけ）か、|角| < ``min_angle`` なら**補正しない**（整った画像を無駄に再標本化しない。
      PNG のバイト列もそのまま返す）
    - 回転後の画像は**元の寸法のまま**（expand=False）。四隅は白で埋まり、端の数 px は
      切れ得る。寸法を変えないのは pages.width/height と座標系の正（DD-01）を単純に
      保つため。角度は反時計回りが正（Pillow の rotate と同じ向き）で、適用した回転を
      ``preproc["angle"]`` に残す
    - 画像を開けない・Pillow が無いときは warning を出して無補正で通す（取込を止めない）
    - ``apply=False``（``INGEST_PREPROCESS=deskew_measure``）は**推定だけ**して回さない。
      画像のバイト列は常にそのまま返し、``preproc["deskew"]`` に推定角と「補正するなら
      回したか」（``would_apply``）を残す。回転の再標本化は小さい文字を崩しうる
      （sample2 を 2° 傾けた合成で、補正ありは 2 文字誤読・補正なしは正読。
      docs/design/dd01-deskew-measurement-2026-09-13.md）ので、実帳票の傾き分布は
      OCR に触らずに測る
    """

    def __init__(
        self,
        *,
        apply: bool = True,
        max_angle: float = 5.0,
        coarse_step: float = 1.0,
        fine_step: float = 0.25,
        min_angle: float = 0.3,
        min_gain: float = 1.05,
        work_width: int = 800,
    ) -> None:
        self._max_angle = max_angle
        self._coarse_step = coarse_step
        self._fine_step = fine_step
        self._min_angle = min_angle
        self._min_gain = min_gain
        self._work_width = work_width
        self._apply = apply

    # ---- 推定 ----
    def estimate_angle(self, png_bytes: bytes) -> tuple[float, dict[str, Any]]:
        """適用すべき回転角（度、反時計回りが正）と判定の内訳を返す。補正不要なら 0.0。"""
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(png_bytes)) as im:
            g = im.convert("L")
            if g.width > self._work_width:
                g = g.resize(
                    (self._work_width, max(1, round(g.height * self._work_width / g.width))),
                    Image.Resampling.BILINEAR,
                )
            inv = ImageOps.invert(g)

            def score(angle: float) -> float:
                rot = (
                    inv
                    if angle == 0.0
                    else inv.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False, fillcolor=0)
                )
                # 幅 1 px への BOX 縮小 = 各行の平均（射影プロファイル）
                rows = rot.resize((1, rot.height), Image.Resampling.BOX).tobytes()
                n = len(rows)
                if n == 0:
                    return 0.0
                mean = float(sum(rows)) / n
                return float(sum((v - mean) ** 2 for v in rows)) / n

            scores: dict[float, float] = {}
            for a in _frange(-self._max_angle, self._max_angle, self._coarse_step):
                scores[a] = score(a)
            best = max(scores, key=lambda k: scores[k])
            lo = max(-self._max_angle, best - self._coarse_step)
            hi = min(self._max_angle, best + self._coarse_step)
            for a in _frange(lo, hi, self._fine_step):
                if a not in scores:
                    scores[a] = score(a)
            best = max(scores, key=lambda k: scores[k])
            score0 = scores.get(0.0) or score(0.0)
            gain = (scores[best] / score0) if score0 > 0 else 0.0

        detail: dict[str, Any] = {
            "method": "projection_profile",
            "estimated": round(best, 2),
            "gain": round(gain, 4),
            "work_width": self._work_width,
        }
        if abs(best) < self._min_angle or gain < self._min_gain:
            detail["applied"] = False
            return 0.0, detail
        detail["applied"] = True
        return float(best), detail

    # ---- 前処理 ----
    def preprocess(self, page: RasterPage, *, source_hint: str = "unknown") -> PreprocessedPage:
        meta: dict[str, object] = {"angle": 0, "unwarp": False, "scale": 1.0}
        try:
            angle, detail = self.estimate_angle(page.png_bytes)
        except Exception as exc:  # noqa: BLE001 - 前処理の失敗で取込を止めない
            logger.warning("[deskew] 傾き推定に失敗（無補正で継続）: page=%s err=%s", page.page_no, exc)
            meta["deskew"] = {"error": str(exc)[:200], "applied": False}
            return PreprocessedPage(page.page_no, page.width, page.height, page.png_bytes, meta)
        if not self._apply:
            # 測るだけ: 回さない。「補正するなら回したか」は would_apply に残す
            meta["deskew"] = {**detail, "applied": False, "would_apply": angle != 0.0, "measure_only": True}
            return PreprocessedPage(page.page_no, page.width, page.height, page.png_bytes, meta)
        meta["deskew"] = detail
        if angle == 0.0:
            return PreprocessedPage(page.page_no, page.width, page.height, page.png_bytes, meta)

        from PIL import Image

        with Image.open(io.BytesIO(page.png_bytes)) as im:
            src = im.convert("RGB") if im.mode not in ("L", "RGB") else im.copy()
            fill: Any = 255 if src.mode == "L" else (255, 255, 255)
            fixed = src.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=fill)
            buf = io.BytesIO()
            fixed.save(buf, format="PNG")
        meta["angle"] = round(angle, 2)
        return PreprocessedPage(page.page_no, fixed.width, fixed.height, buf.getvalue(), meta)


def preprocessor_from_env(env: Optional[Mapping[str, str]] = None) -> Preprocessor:
    """``INGEST_PREPROCESS`` から前処理を選ぶ。

    - 空 / ``none``（既定）: ``NoopPreprocessor``
    - ``deskew``: ``DeskewPreprocessor``（射影プロファイルの傾き補正）
    - ``deskew_measure``: 傾きを推定して ``preproc.deskew`` に残すだけ（画像は回さない）。
      本番で補正を有効にする前に、実帳票の傾き分布を OCR に影響させずに測るためのもの
    - それ以外: warning を出して Noop（取込を止めない）
    """
    src = os.environ if env is None else env
    value = (src.get("INGEST_PREPROCESS") or "").strip().lower()
    if value in ("", "none", "0", "off", "false", "no"):
        return NoopPreprocessor()
    if value == "deskew":
        return DeskewPreprocessor()
    if value == "deskew_measure":
        return DeskewPreprocessor(apply=False)
    logger.warning(
        "INGEST_PREPROCESS=%r は未対応です。前処理なしで続けます（none|deskew|deskew_measure）", value
    )
    return NoopPreprocessor()
